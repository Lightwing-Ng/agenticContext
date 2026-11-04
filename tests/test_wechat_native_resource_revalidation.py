"""Native placeholder fallback retains exact source and existing-text evidence.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.wechat_archive import (
    WechatArchiveRepository,
    _file_identity,
    _identifier,
    _time_fields,
    wechat_archive_path,
    wechat_history_partition_fingerprint,
)
from app.core.wechat_native_resource_revalidation import (
    select_resource_index,
    stage_native_resource_revalidation,
)
from app.core.wechat_resource_index import _SCHEMA
from app.core.wechat_session_export import (
    WechatSessionExport,
    WechatSessionSourceChanged,
)
from tests.test_wechat_archive import make_archive
from app.core.wechat_archive import import_wechat_archive


OWNER = "wxid_account_a"
BACKUP = "a" * 64
DATABASE = "b" * 64


@pytest.fixture
def native_metadata(tmp_path):
    store = tmp_path / "store"
    import_wechat_archive(make_archive(tmp_path / "archive"), store)
    history = wechat_archive_path(store)
    with sqlite3.connect(history) as connection:
        connection.row_factory = sqlite3.Row
        conversation = dict(
            connection.execute(
                "SELECT * FROM conversations WHERE account_key=? AND kind='direct'",
                ("account:" + OWNER,),
            ).fetchone()
        )
        pid = conversation["partition_id"]
        relative = "Documents/" + pid[4:] + "/DB/message_2.sqlite"
        metadata = dict(
            source_id=pid,
            source_account_id=OWNER,
            account_hash=pid[4:],
            account_hash_verified=True,
            backup_id=BACKUP,
            input_databases=[dict(path=relative, sha256=DATABASE)],
        )
        connection.execute(
            "CREATE TABLE source_snapshots (id INTEGER PRIMARY KEY,snapshot_id TEXT NOT NULL,source_id TEXT NOT NULL,metadata TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO source_snapshots VALUES(1,?,?,?)",
            (BACKUP, pid, json.dumps(metadata)),
        )
        table = (
            "Chat_"
            + hashlib.md5(
                conversation["peer_id"].encode(), usedforsecurity=False
            ).hexdigest()
        )
        resources = []
        for local, kind, typ in ((1, "app", 49), (2, "image", 3), (3, "file", 49)):
            epoch = 1650000000 + local
            source_path = "AppDomain-com.tencent.xin/" + relative
            identifier = _identifier(
                "wechat-resource-reference-v1",
                OWNER,
                BACKUP,
                source_path,
                table,
                str(local),
            )
            label = {"app": "[App message]", "image": "[Image]", "file": "[File]"}[kind]
            proof = dict(
                record_id=identifier,
                backup_id=BACKUP,
                source_sha256=DATABASE,
                source_checksum_basis="preservation_manifest",
                source_relative_path=source_path,
                locator=dict(
                    database="message_2.sqlite",
                    table=table,
                    local_id=local,
                    row_id=local,
                    text_origin="native_resource_metadata",
                ),
                server_id=str(10000 + local),
                unix_timestamp=epoch,
                message_type=typ,
                text_extraction_kind="resource_placeholder",
                resource_reference=dict(
                    account_id=OWNER,
                    peer_id=conversation["peer_id"],
                    unix_timestamp=epoch,
                    kind=kind,
                    label=label,
                ),
            )
            time = _time_fields(
                {"sent_at_utc_iso": datetime.fromtimestamp(epoch, UTC).isoformat()}
            )
            resources.append(
                (
                    identifier,
                    conversation["id"],
                    local,
                    conversation["peer_id"],
                    "Recorded sender",
                    "received",
                    label,
                    *time,
                    json.dumps(proof),
                    '["resource_placeholder","media_payload_not_read"]',
                    label.lower(),
                )
            )
        primary_id = connection.execute(
            "SELECT id FROM messages WHERE conversation_id=?", (conversation["id"],)
        ).fetchone()[0]
        primary_proof = deepcopy(json.loads(resources[0][11]))
        primary_proof.pop("resource_reference")
        primary_proof.update(
            source_snapshot=1,
            record_id="native-app",
            text_extraction_kind="text_appmsg",
        )
        primary_proof["locator"]["text_origin"] = "native_fts_exact_key_invitation_card"
        connection.execute(
            "UPDATE messages SET provenance=?,direction='received' WHERE id=?",
            (json.dumps(primary_proof), primary_id),
        )
    original = history.with_name("resource_references.sqlite3")
    with sqlite3.connect(original) as output, sqlite3.connect(history) as primary:
        output.executescript(_SCHEMA)
        # The original publication predates another primary revision.
        output.executemany(
            "INSERT INTO archive_info VALUES(?,?)",
            [
                ("schema_version", "1"),
                ("history_file_identity", json.dumps(_file_identity(history))),
                (
                    "history_partitions_sha256",
                    wechat_history_partition_fingerprint(primary),
                ),
            ],
        )
        output.executemany(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", resources
        )
        first, last = resources[0], resources[-1]
        output.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                conversation[key]
                for key in (
                    "id",
                    "partition_id",
                    "account_key",
                    "peer_id",
                    "title",
                    "kind",
                )
            )
            + (3, first[7], last[7], first[8], last[8], last[6]),
        )
    with sqlite3.connect(history) as connection:
        connection.execute(
            "INSERT INTO archive_info VALUES('later_primary_revision','preserved')"
        )
    return SimpleNamespace(
        store=store,
        history=history,
        original=original,
        conversation=conversation,
        resources=resources,
        metadata=metadata,
        primary_id=primary_id,
    )


def _stage(fixture):
    candidate = fixture.original.with_name("candidate.sqlite3")
    return candidate, stage_native_resource_revalidation(
        fixture.store, candidate, conversation_ids=[fixture.conversation["id"]]
    )


def test_verified_scope_keeps_originals_excludes_only_same_native_event_and_exports(
    native_metadata,
):
    fixture = native_metadata
    before = fixture.history.read_bytes(), fixture.original.read_bytes()
    candidate, report = _stage(fixture)
    assert report["messages"] == 2
    assert report["excluded_existing_events"] == [
        {
            "resource_id": fixture.resources[0][0],
            "primary_ids": [fixture.primary_id],
            "basis": "same_snapshot_database_sha256_table_row_server_time_type_direction",
        }
    ]
    assert report["media_payload_files_read"] == report["cloud_inputs_hydrated"] == 0
    assert not report["published"]
    fallback = fixture.history.with_name("resource_revalidated.sqlite3")
    candidate.replace(fallback)
    assert select_resource_index(fixture.history) == fixture.original
    assert (
        select_resource_index(
            fixture.history, conversation_id=fixture.conversation["id"]
        )
        == fallback
    )
    assert (
        select_resource_index(fixture.history, conversation_id="wechat:" + "f" * 64)
        == fixture.original
    )
    page = WechatArchiveRepository(fixture.store).query(
        session=fixture.conversation["id"]
    )
    assert page["resource_index_status"] == "ready"
    assert page["count"] == 3
    assert {row["id"] for row in page["messages"]} == {
        fixture.primary_id,
        fixture.resources[1][0],
        fixture.resources[2][0],
    }
    export = WechatSessionExport(fixture.store, fixture.conversation["id"])
    assert fallback in export.sources and fixture.original not in export.sources
    assert len(list(export.items())) == 3
    assert (
        WechatArchiveRepository(fixture.store).resource_message(
            fixture.resources[1][0]
        )["id"]
        == fixture.resources[1][0]
    )
    assert (fixture.history.read_bytes(), fixture.original.read_bytes()) == before


def test_sealed_fallback_rejects_modified_display_body(native_metadata):
    fixture = native_metadata
    candidate, _ = _stage(fixture)
    fallback = fixture.history.with_name("resource_revalidated.sqlite3")
    candidate.replace(fallback)
    with sqlite3.connect(fallback) as source:
        source.execute(
            "UPDATE messages SET text='FORGED MESSAGE' WHERE id=?",
            (fixture.resources[1][0],),
        )
    assert (
        select_resource_index(
            fixture.history, conversation_id=fixture.conversation["id"]
        )
        == fixture.original
    )
    page = WechatArchiveRepository(fixture.store).query(
        session=fixture.conversation["id"]
    )
    assert page["resource_index_status"] == "stale"
    assert all(row["text"] != "FORGED MESSAGE" for row in page["messages"])


def test_unknown_zero_server_id_retains_exact_native_row_matching(native_metadata):
    fixture = native_metadata
    with sqlite3.connect(fixture.history) as primary:
        proof = json.loads(primary.execute(
            "SELECT provenance FROM messages WHERE id=?", (fixture.primary_id,)
        ).fetchone()[0])
        proof["server_id"] = "0"
        primary.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(proof), fixture.primary_id),
        )
    with sqlite3.connect(fixture.original) as source:
        proof = json.loads(source.execute(
            "SELECT provenance FROM messages WHERE id=?", (fixture.resources[0][0],)
        ).fetchone()[0])
        proof["server_id"] = None
        source.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(proof), fixture.resources[0][0]),
        )
    _, report = _stage(fixture)
    assert report["messages"] == 2
    assert report["excluded_existing_events"][0]["primary_ids"] == [fixture.primary_id]


@pytest.mark.parametrize("server", ["00", "-1", "1.0", 0, True, "9223372036854775808"])
def test_other_invalid_server_identifiers_remain_rejected(native_metadata, server):
    fixture = native_metadata
    with sqlite3.connect(fixture.history) as primary:
        proof = json.loads(primary.execute(
            "SELECT provenance FROM messages WHERE id=?", (fixture.primary_id,)
        ).fetchone()[0])
        proof["server_id"] = server
        primary.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(proof), fixture.primary_id),
        )
    with pytest.raises(ValueError, match="server ID"):
        _stage(fixture)


@pytest.mark.parametrize(
    "defect",
    [
        "sha",
        "path",
        "account",
        "peer",
        "table",
        "server",
        "clock",
        "type",
        "row",
        "timestamp",
    ],
)
def test_native_scope_rejects_unproved_or_conflicting_evidence(native_metadata, defect):
    fixture = native_metadata
    with sqlite3.connect(fixture.original) as source:
        raw = source.execute(
            "SELECT provenance FROM messages WHERE id=?", (fixture.resources[0][0],)
        ).fetchone()[0]
        proof = json.loads(raw)
        if defect == "sha":
            proof["source_sha256"] = "c" * 64
        elif defect == "path":
            proof["source_relative_path"] = "Documents/wrong/DB/message_2.sqlite"
        elif defect == "account":
            proof["resource_reference"]["account_id"] = "wxid_other"
        elif defect == "peer":
            proof["resource_reference"]["peer_id"] = "wxid_other"
        elif defect == "table":
            proof["locator"]["table"] = "Chat_" + "d" * 32
        elif defect == "server":
            proof["server_id"] = "30000"
        elif defect == "clock":
            proof["unix_timestamp"] += 1
            proof["resource_reference"]["unix_timestamp"] += 1
        elif defect == "type":
            proof["message_type"] = 3
        elif defect == "row":
            proof["locator"]["row_id"] += 1
        elif defect == "timestamp":
            source.execute(
                "UPDATE messages SET timestamp_value='2000-01-01T00:00:00+08:00' WHERE id=?",
                (fixture.resources[0][0],),
            )
        source.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(proof), fixture.resources[0][0]),
        )
    before = fixture.history.read_bytes(), fixture.original.read_bytes()
    with pytest.raises(ValueError):
        _stage(fixture)
    assert (fixture.history.read_bytes(), fixture.original.read_bytes()) == before


def test_fallback_never_masks_a_changed_primary_or_source_proof(native_metadata):
    fixture = native_metadata
    candidate, _ = _stage(fixture)
    fallback = fixture.history.with_name("resource_revalidated.sqlite3")
    candidate.replace(fallback)
    export = WechatSessionExport(fixture.store, fixture.conversation["id"])
    with sqlite3.connect(fallback) as source:
        source.execute(
            "UPDATE revalidation_source_proofs SET metadata_sha256=?", ("f" * 64,)
        )
    assert (
        select_resource_index(
            fixture.history, conversation_id=fixture.conversation["id"]
        )
        == fixture.original
    )
    with pytest.raises(WechatSessionSourceChanged):
        next(export.items())
    export.close()


def test_current_full_index_remains_preferred_over_a_scoped_fallback(native_metadata):
    fixture = native_metadata
    candidate, _ = _stage(fixture)
    candidate.replace(fixture.history.with_name("resource_revalidated.sqlite3"))
    with sqlite3.connect(fixture.original) as source:
        source.execute(
            "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
            (json.dumps(_file_identity(fixture.history)),),
        )
    assert select_resource_index(fixture.history) == fixture.original


def test_verified_scope_works_without_opening_cloud_only_original(
    native_metadata, monkeypatch
):
    fixture = native_metadata
    candidate, _ = _stage(fixture)
    fallback = fixture.history.with_name("resource_revalidated.sqlite3")
    candidate.replace(fallback)
    original_lstat, original_stat = Path.lstat, Path.stat
    original_connect = sqlite3.connect

    def cloud_state(path, follow_symlinks=True):
        state = original_stat(path, follow_symlinks=follow_symlinks)
        if path == fixture.original:
            return SimpleNamespace(
                **{
                    key: getattr(state, key)
                    for key in dir(state)
                    if key.startswith("st_") and key != "st_flags"
                },
                st_flags=0x40000000,
            )
        return state

    def cloud_lstat(path):
        if path == fixture.original:
            return cloud_state(path, follow_symlinks=False)
        return original_lstat(path)

    def no_original_open(path, *args, **kwargs):
        assert "resource_references.sqlite3" not in str(path)
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", cloud_state)
    monkeypatch.setattr(Path, "lstat", cloud_lstat)
    monkeypatch.setattr(sqlite3, "connect", no_original_open)
    page = WechatArchiveRepository(fixture.store).query(
        session=fixture.conversation["id"]
    )
    assert page["count"] == 3 and page["resource_index_status"] == "ready"
    export = WechatSessionExport(fixture.store, fixture.conversation["id"])
    assert fallback in export.sources
    assert len(list(export.items())) == 3
