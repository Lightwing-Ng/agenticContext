"""Verify source-bound resource author recovery without media or input mutation.

Code version: v1.1.1-codex.0
"""

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.core import wechat_native_resource_senders as senders
from app.core.wechat_archive import (
    _SCHEMA as HISTORY_SCHEMA,
    _file_identity,
    _identifier,
    _time_fields,
    wechat_history_partition_fingerprint,
)
from app.core.wechat_resource_index import _SCHEMA as RESOURCE_SCHEMA


OWNER = "wxid_resource_owner"
PEER = "123456@chatroom"
SENDER = "wxid_exact_author"
EPOCH = 1_700_000_002


def _insert(connection, table, values):
    connection.execute(
        f"INSERT INTO {table} VALUES({','.join('?' for _ in values)})", values
    )


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def archive(tmp_path):
    def build(payload=None, marker=0, *, sender=None):
        payload = (
            payload if payload is not None else SENDER + ":\n<msg><videomsg/></msg>"
        )
        native = tmp_path / "native.sqlite3"
        table = "Chat_" + hashlib.md5(PEER.encode(), usedforsecurity=False).hexdigest()
        with sqlite3.connect(native) as connection:
            connection.execute(
                f'CREATE TABLE "{table}"(MesLocalID INTEGER PRIMARY KEY,'
                "MesSvrID INTEGER,CreateTime INTEGER,Type INTEGER,Des INTEGER,"
                "Message BLOB,WCDB_CT_Message INTEGER)"
            )
            connection.execute(
                f'INSERT INTO "{table}" VALUES(2,202,?,43,1,?,?)',
                (EPOCH, payload, marker),
            )
        digest = _hash(native)
        hashed = hashlib.md5(OWNER.encode(), usedforsecurity=False).hexdigest()
        partition = "ios-" + hashed
        relative = f"Documents/{hashed}/DB/message_1.sqlite"
        file_id = hashlib.sha1(
            ("AppDomain-com.tencent.xin-" + relative).encode(), usedforsecurity=False
        ).hexdigest()
        backup = "independent-backup"
        cid = "wechat:" + _identifier(partition, PEER)
        metadata = dict(
            source_id=partition,
            source_account_id=OWNER,
            account_hash_verified=True,
            account_hash=hashed,
            snapshot_id=backup,
            identity_evidence=dict(directory_hash=hashed),
        )
        locator = dict(
            database="message_1.sqlite",
            database_relative_path=relative,
            database_sha256=digest,
            file_id=file_id,
            table=table,
            local_id=1,
            row_id=1,
        )
        anchor = dict(
            source_snapshot=1,
            server_id="101",
            unix_timestamp=EPOCH - 1,
            message_type=1,
            text_extraction_kind="plain_text",
            locator=locator,
        )
        clock = _time_fields(
            {"sent_at_utc_iso": datetime.fromtimestamp(EPOCH, UTC).isoformat()}
        )
        conversation = (
            cid,
            partition,
            "account:" + OWNER,
            PEER,
            "Test group",
            "group",
            1,
            *clock[:1],
            *clock[:1],
            clock[1],
            clock[1],
            "Anchor",
        )
        history = tmp_path / "history.sqlite3"
        with sqlite3.connect(history) as connection:
            connection.executescript(HISTORY_SCHEMA)
            connection.execute(
                "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,source_id TEXT,metadata TEXT)"
            )
            _insert(
                connection,
                "partitions",
                (
                    partition,
                    "account:" + OWNER,
                    OWNER,
                    "Owner",
                    "Verified archive ID",
                    "iPad archive",
                    "Preserved source",
                    "",
                ),
            )
            _insert(connection, "conversations", conversation)
            _insert(
                connection,
                "source_snapshots",
                (1, backup, partition, json.dumps(metadata)),
            )
            _insert(
                connection,
                "messages",
                (
                    "primary-anchor",
                    cid,
                    1,
                    SENDER,
                    SENDER,
                    "received",
                    "Anchor",
                    *clock,
                    json.dumps(anchor),
                    "[]",
                    "anchor",
                ),
            )
        identifier = _identifier(
            "wechat-resource-reference-v2",
            OWNER,
            backup,
            "AppDomain-com.tencent.xin/" + relative,
            digest,
            table,
            "2",
        )
        proof = dict(
            record_id=identifier,
            record_id_scheme="wechat-resource-reference-v2",
            source_snapshot=1,
            source_snapshot_metadata_sha256=hashlib.sha256(
                json.dumps(metadata).encode()
            ).hexdigest(),
            backup_id=backup,
            source_relative_path="AppDomain-com.tencent.xin/" + relative,
            source_sha256=digest,
            source_checksum_basis="primary_source_database_receipt",
            server_id="202",
            unix_timestamp=EPOCH,
            message_type=43,
            text_extraction_kind="resource_placeholder",
            locator={
                **locator,
                "local_id": 2,
                "row_id": 2,
                "text_origin": "native_resource_metadata",
            },
            recorded_native_metadata=dict(
                MesLocalID=2, MesSvrID="202", CreateTime=EPOCH, Type=43, Des=1
            ),
            resource_reference=dict(
                kind="video",
                label="[Video]",
                account_id=OWNER,
                peer_id=PEER,
                unix_timestamp=EPOCH,
            ),
        )
        catalog = tmp_path / "catalog.sqlite3"
        with (
            sqlite3.connect(catalog) as connection,
            sqlite3.connect(history) as primary,
        ):
            connection.executescript(RESOURCE_SCHEMA)
            connection.execute("CREATE TABLE native_resource_occurrences(proof TEXT)")
            connection.execute(
                "INSERT INTO native_resource_occurrences VALUES('retained')"
            )
            _insert(connection, "conversations", conversation)
            flags = ["resource_placeholder", "media_payload_not_read"]
            if sender is None:
                flags.append("sender_id_unavailable")
            _insert(
                connection,
                "messages",
                (
                    identifier,
                    cid,
                    2,
                    sender,
                    sender or "Unknown sender",
                    "received",
                    "[Video]",
                    *clock,
                    json.dumps(proof),
                    json.dumps(flags),
                    "[video]",
                ),
            )
            connection.executemany(
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
        return SimpleNamespace(
            history=history, catalog=catalog, native=native, id=identifier, table=table
        )

    return build


def _recover(item, **kwargs):
    return senders.recover_native_resource_sender(
        item.history, item.catalog, item.id, item.native, **kwargs
    )


def _change_proof(item, change):
    with sqlite3.connect(item.catalog) as connection:
        proof = json.loads(
            connection.execute("SELECT provenance FROM messages").fetchone()[0]
        )
        change(proof)
        connection.execute("UPDATE messages SET provenance=?", (json.dumps(proof),))


def test_plain_video_prefix_resolves_without_mutating_sources_or_media(archive):
    item = archive()
    before = {p: _hash(p) for p in (item.history, item.catalog, item.native)}
    result = _recover(item)
    row, resolution = result["message"], result["resolution"]
    assert row["sender_id"] == SENDER
    assert row["sender_label"] == SENDER
    assert row["text"] == "[Video]"
    assert row["id"] == item.id
    assert row["direction"] == "received"
    assert resolution["status"] == "resolved"
    assert resolution["basis"] == "exact_native_group_message_prefix"
    assert resolution["media_payload_files_read"] == 0
    assert result["published"] is False
    flags = json.loads(row["quality_flags"])
    assert "sender_id_unavailable" not in flags
    assert "media_payload_not_read" in flags
    assert SENDER in row["search_text"]
    assert {p: _hash(p) for p in before} == before
    with sqlite3.connect(item.catalog) as connection:
        assert connection.execute(
            "SELECT proof FROM native_resource_occurrences"
        ).fetchall() == [("retained",)]


@pytest.mark.parametrize(
    "payload",
    [
        "<msg><videomsg fromusername='wxid_guess'/></msg>",
        "123456@chatroom:\n<msg/>",
        "prefix text\nwxid_guess:\n<msg/>",
    ],
)
def test_non_prefix_content_does_not_infer_an_author(archive, payload):
    result = _recover(archive(payload))
    assert result["message"]["sender_id"] is None
    assert result["resolution"]["status"] == "prefix_unavailable"
    assert result["resolution"]["basis"] == "source_bound_native_message_decode_attempt"


def test_corrupt_payload_keeps_unknown_with_a_specific_reason(archive):
    result = _recover(archive(b"\xff", 0))
    assert result["message"]["sender_id"] is None
    assert result["resolution"]["status"] == "invalid_payload"
    assert "native_sender_payload_invalid" in json.loads(
        result["message"]["quality_flags"]
    )


@pytest.fixture
def compressed():
    backend = pytest.importorskip("compression.zstd")
    samples = [
        f"wxid_sample_{i}:\n<msg><videomsg repeated='same metadata {i % 7}'/></msg>".encode()
        for i in range(200)
    ]
    trained = backend.train_dict(samples, 1024)
    data = bytearray(trained.dict_content)
    data[4:8] = (5).to_bytes(4, "little")
    dictionary = backend.ZstdDict(bytes(data))
    payload = backend.compress(
        (SENDER + ":\n<msg><videomsg/></msg>").encode(), zstd_dict=dictionary
    )
    return payload, bytes(data)


def test_missing_dictionary_five_stays_unknown_and_preserves_dictionary_id(
    archive, compressed
):
    payload, _ = compressed
    result = _recover(archive(payload, 2))
    assert result["resolution"]["status"] == "missing_dictionary"
    assert result["resolution"]["dictionary_id"] == 5
    assert (
        result["resolution"]["basis"]
        == "native_compression_frame_dictionary_requirement"
    )
    assert (
        result["resolution"]["attempted_method"] == "exact_native_group_message_prefix"
    )
    assert result["message"]["sender_id"] is None
    assert "native_sender_dictionary_unavailable" in json.loads(
        result["message"]["quality_flags"]
    )


def test_correct_dictionary_resolves_and_records_the_dictionary_digest(
    archive, compressed
):
    payload, dictionary = compressed
    result = _recover(archive(payload, 2), dictionaries={5: dictionary})
    assert result["message"]["sender_id"] == SENDER
    assert result["resolution"]["dictionary_id"] == 5
    assert (
        result["resolution"]["dictionary_sha256"]
        == hashlib.sha256(dictionary).hexdigest()
    )


def test_failed_recheck_retains_exact_prior_success_and_its_dictionary_proof(
    archive, compressed
):
    payload, dictionary = compressed
    item = archive(payload, 2)
    initial = _recover(item, dictionaries={5: dictionary})["message"]

    def retain(row):
        with sqlite3.connect(item.catalog) as connection:
            connection.execute(
                "UPDATE messages SET "
                + ",".join(key + "=?" for key in row)
                + " WHERE id=?",
                (*row.values(), item.id),
            )

    retain(initial)
    failed = _recover(item)
    proof = json.loads(failed["message"]["provenance"])
    assert failed["resolution"]["status"] == "missing_dictionary"
    assert failed["message"]["sender_id"] == SENDER
    assert (
        proof["native_resource_sender_resolution"]
        == json.loads(initial["provenance"])["native_resource_sender_resolution"]
    )
    assert (
        proof["native_resource_sender_resolution"]["dictionary_sha256"]
        == hashlib.sha256(dictionary).hexdigest()
    )
    assert proof["native_resource_sender_latest_attempt"] == failed["resolution"]
    retain(failed["message"])
    restored = _recover(item, dictionaries={5: dictionary})["message"]
    assert "native_resource_sender_latest_attempt" not in json.loads(
        restored["provenance"]
    )
    assert restored == initial


def test_missing_backend_is_an_explicit_portable_status(archive, monkeypatch):
    item = archive(b"compressed", 2)

    def unavailable(*args, **kwargs):
        raise senders.wechat_wcdb.WCDBBackendUnavailableError("unavailable")

    monkeypatch.setattr(senders.wechat_wcdb, "decode_wcdb_text", unavailable)
    result = _recover(item)
    assert result["resolution"]["status"] == "backend_unavailable"
    assert result["resolution"]["basis"] == "source_bound_native_message_decode_attempt"
    assert result["message"]["sender_id"] is None


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(server_id="999"),
        lambda p: p.update(message_type=49),
        lambda p: p["recorded_native_metadata"].update(Des=0),
        lambda p: p["recorded_native_metadata"].update(MesSvrID="999"),
        lambda p: p["locator"].update(file_id="bad"),
        lambda p: p.update(source_snapshot=2),
    ],
)
def test_native_event_or_source_disagreement_is_rejected(archive, change):
    item = archive()
    _change_proof(item, change)
    with pytest.raises(ValueError):
        _recover(item)


def test_materialized_source_database_must_match_its_independent_digest(archive):
    item = archive()
    with item.native.open("ab") as stream:
        stream.write(b"changed source")
    with pytest.raises(ValueError, match="independent receipt"):
        _recover(item)


def test_existing_different_author_is_never_overwritten(archive):
    with pytest.raises(ValueError, match="conflicts"):
        _recover(archive(sender="wxid_other_author"))


def test_replaying_a_staged_row_is_idempotent(archive):
    item = archive()
    result = _recover(item)
    row = result["message"]
    columns = tuple(row)
    with sqlite3.connect(item.catalog) as connection:
        connection.execute(
            "UPDATE messages SET "
            + ",".join(column + "=?" for column in columns)
            + " WHERE id=?",
            (*[row[column] for column in columns], item.id),
        )
    assert _recover(item)["message"] == row


def test_stage_preserves_memberships_and_every_input(archive, monkeypatch, tmp_path):
    item = archive()
    monkeypatch.setattr(senders, "_clonefile", shutil.copyfile)
    candidate = tmp_path / "sender-candidate.sqlite3"
    paths = (item.history, item.catalog, item.native)
    before = {path: _hash(path) for path in paths}
    report = senders.stage_native_resource_senders(
        item.history, item.catalog, candidate, item.native, message_ids=[item.id]
    )
    assert report["statuses"] == {"resolved": 1}
    assert report["history_cloned"] is False
    assert report["published"] is False
    assert report["candidate_sha256"] == _hash(candidate)
    assert {path: _hash(path) for path in paths} == before
    with sqlite3.connect(candidate) as connection:
        row = connection.execute("SELECT sender_id,text FROM messages").fetchone()
        assert row == (SENDER, "[Video]")
        assert connection.execute(
            "SELECT * FROM native_resource_occurrences"
        ).fetchall() == [("retained",)]
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]


def test_stage_records_dictionary_blocker_without_fabricating_sender(
    archive, compressed, monkeypatch, tmp_path
):
    item = archive(compressed[0], 2)
    monkeypatch.setattr(senders, "_clonefile", shutil.copyfile)
    candidate = tmp_path / "sender-candidate.sqlite3"
    report = senders.stage_native_resource_senders(
        item.history, item.catalog, candidate, item.native, message_ids=[item.id]
    )
    assert report["statuses"] == {"missing_dictionary": 1}
    with sqlite3.connect(candidate) as connection:
        row = connection.execute("SELECT sender_id,provenance FROM messages").fetchone()
        assert row[0] is None
        resolution = json.loads(row[1])["native_resource_sender_resolution"]
        assert resolution["dictionary_id"] == 5
        assert resolution["status"] == "missing_dictionary"
        assert resolution["basis"] == "native_compression_frame_dictionary_requirement"


def test_stage_rejects_existing_candidate_without_overwriting_it(archive, tmp_path):
    item = archive()
    candidate = tmp_path / "existing.sqlite3"
    candidate.write_bytes(b"user-owned")
    with pytest.raises(ValueError, match="new sender candidate"):
        senders.stage_native_resource_senders(
            item.history, item.catalog, candidate, item.native, message_ids=[item.id]
        )
    assert candidate.read_bytes() == b"user-owned"


@pytest.mark.parametrize(
    "field,value",
    [("account_key", "account:wxid_other"), ("peer_id", "other@chatroom")],
)
def test_different_catalog_account_or_peer_is_rejected(archive, field, value):
    item = archive()
    with sqlite3.connect(item.catalog) as connection:
        connection.execute(f"UPDATE conversations SET {field}=?", (value,))
    with pytest.raises(ValueError, match="source scope"):
        _recover(item)


def test_cli_default_is_read_only_and_reports_resolution(archive):
    item = archive()
    before = {path: _hash(path) for path in (item.history, item.catalog, item.native)}
    command = [
        sys.executable,
        str(
            Path(__file__).resolve().parents[1]
            / "scripts/repair_wechat_resource_senders.py"
        ),
        "--history",
        str(item.history),
        "--catalog",
        str(item.catalog),
        "--native-database",
        str(item.native),
        "--message-id",
        item.id,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=True)
    result = json.loads(completed.stdout)
    assert result["published"] is False
    assert result["resolutions"][0]["sender_id"] == SENDER
    assert {path: _hash(path) for path in before} == before


def test_shared_sender_projection_is_pure_and_rechecks_source_scope(
    archive, monkeypatch
):
    item = archive()
    with (
        sqlite3.connect(item.catalog) as catalog,
        sqlite3.connect(item.native) as native,
    ):
        catalog.row_factory = native.row_factory = sqlite3.Row
        row = dict(catalog.execute("SELECT * FROM messages").fetchone())
        conversation = dict(catalog.execute("SELECT * FROM conversations").fetchone())
        value = dict(
            native.execute(
                "SELECT MesLocalID,CAST(MesSvrID AS TEXT) AS MesSvrID,CreateTime,Type,Des,"
                f'Message,WCDB_CT_Message AS compression_marker FROM "{item.table}"'
            ).fetchone()
        )
    proof = json.loads(row["provenance"])

    def no_io(*args, **kwargs):
        pytest.fail("The shared source-bound projection must perform no file I/O.")

    monkeypatch.setattr(senders, "_readonly", no_io)
    monkeypatch.setattr(senders, "_resident", no_io)
    monkeypatch.setattr(senders, "_sha256", no_io)
    updated, resolution = senders.resolve_source_bound_resource_sender(
        row, proof, value, conversation=conversation, owner=OWNER
    )
    assert updated["sender_id"] == SENDER
    assert resolution["status"] == "resolved"
    with pytest.raises(ValueError, match="account, peer, or source row"):
        senders.resolve_source_bound_resource_sender(
            row,
            proof,
            value,
            conversation=conversation | {"account_key": "account:wxid_other"},
            owner=OWNER,
        )
