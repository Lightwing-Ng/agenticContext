"""Native resource catalogs preserve accounts, source bytes, and undecoded records.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.owner_only_files import owner_only_problem
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_resource_index import build_resource_index, resource_index_path
from tests.test_wechat_archive import make_archive


OWNER = "wxid_account_a"
BACKUP = "a" * 64
SERVER = 9223372036854775790
NOW = 1778480608


def _table(peer):
    return "Chat_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()


@pytest.fixture
def resources(tmp_path):
    archive = make_archive(tmp_path / "archive")
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    snapshot = tmp_path / "preserved" / "snapshot"
    owner_root = (
        snapshot
        / "AppDomain-com.tencent.xin"
        / "Documents"
        / hashlib.md5(OWNER.encode(), usedforsecurity=False).hexdigest()
    )
    db_root = owner_root / "DB"
    db_root.mkdir(parents=True)
    (snapshot.parent / "preservation-manifest.json").write_text(
        json.dumps({"snapshot_id": BACKUP, "selected_files": []})
    )
    database = db_root / "message_1.sqlite"
    columns = "(CreateTime INTEGER,Des INTEGER,MesLocalID INTEGER PRIMARY KEY,Message TEXT,MesSvrID INTEGER,Type INTEGER,WCDB_CT_Message INTEGER)"
    with closing(sqlite3.connect(database)) as connection, connection:
        for peer in ("peer-direct", "peer-group", "attachments@chatroom"):
            connection.execute(f'CREATE TABLE "{_table(peer)}" {columns}')
        rows = [
            (NOW, 1, 10, b"dictionary-compressed-image", SERVER, 3, 2),
            (NOW + 1, 0, 11, b"dictionary-compressed-video", SERVER - 1, 43, 2),
            (
                NOW + 2,
                1,
                12,
                "<msg><appmsg><type>6</type><title>report &amp; data.pdf</title><appattach><md5>"
                + "b" * 32
                + "</md5><totallen>4321</totallen></appattach></appmsg></msg>",
                SERVER - 2,
                49,
                0,
            ),
            (NOW + 3, 1, 13, b"dictionary-compressed-app", SERVER - 3, 49, 2),
            (
                NOW + 4,
                1,
                14,
                "A source text remains in its text index",
                SERVER - 4,
                1,
                0,
            ),
            (
                NOW + 5,
                1,
                15,
                "A system notice remains in its text index",
                SERVER - 5,
                10000,
                0,
            ),
            (NOW + 6, 1, 16, b"not-opened-voice", SERVER - 6, 34, 2),
            (NOW + 7, 1, 17, b"not-opened-sticker", SERVER - 7, 47, 2),
            (NOW + 8, 1, 18, b"not-opened-unknown", SERVER - 8, 64, 2),
        ]
        connection.executemany(
            f'INSERT INTO "{_table("peer-direct")}" VALUES(?,?,?,?,?,?,?)', rows
        )
        connection.execute(
            f'INSERT INTO "{_table("peer-group")}" VALUES(?,?,?,?,?,?,?)',
            (NOW, 1, 10, b"not-decoded-group", SERVER, 3, 2),
        )
        connection.execute(
            f'INSERT INTO "{_table("attachments@chatroom")}" VALUES(?,?,?,?,?,?,?)',
            (NOW, 1, 10, b"only-resource-conversation", SERVER, 43, 2),
        )
    with closing(sqlite3.connect(db_root / "MM.sqlite")) as connection, connection:
        connection.execute("CREATE TABLE Friend(UsrName TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO Friend VALUES('attachments@chatroom')")
    (owner_root / "FileIndex").mkdir()
    with closing(sqlite3.connect(owner_root / "FileIndex/fileIndexInfo.db")) as connection, connection:
        connection.execute(
            "CREATE TABLE FileIndexInfoTable(fileMD5 TEXT,filePath TEXT,fileSize INTEGER,indexType INTEGER,msgLocalId INTEGER,msgtime INTEGER,msgType INTEGER,userName TEXT)"
        )
        connection.executemany(
            "INSERT INTO FileIndexInfoTable VALUES(?,?,?,?,?,?,?,?)",
            [
                ("c" * 32, "&I/10.pic_thum", 50, 11, 10, NOW, 3, "peer-direct"),
                ("d" * 32, "&I/10.pic", 100, 10, 10, NOW, 3, "peer-direct"),
                (
                    "e" * 32,
                    "mini-program.wxam",
                    200,
                    30,
                    13,
                    NOW + 3,
                    49,
                    "peer-direct",
                ),
            ],
        )
    (owner_root / "FileBrowse").mkdir()
    with closing(sqlite3.connect(owner_root / "FileBrowse/fileInfo.db")) as connection, connection:
        connection.execute(
            "CREATE TABLE BrowseInfoTable(msgWrapSvrID INTEGER PRIMARY KEY,msgLocalID INTEGER,fileTime INTEGER,desTitle TEXT,fileExt TEXT,fileMD5 TEXT,fileSize INTEGER,fromUserName TEXT,fromChatName TEXT)"
        )
    return SimpleNamespace(
        store=store,
        snapshot=snapshot,
        source=database,
        owner_root=owner_root,
        history=wechat_archive_path(store),
    )


def _rows(fixture):
    with sqlite3.connect(resource_index_path(fixture.store)) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM messages ORDER BY conversation_id,ordinal"
            )
        ]


def test_native_types_and_real_times_become_metadata_placeholders_without_payload_reads(
    resources,
):
    fixture = resources
    source_before, history_before = (
        fixture.source.read_bytes(),
        fixture.history.read_bytes(),
    )
    report = build_resource_index(fixture.store, fixture.snapshot)
    assert report["messages"] == 9
    assert report["conversations"] == 3
    assert report["media_payload_files_read"] == 0
    assert report["kinds"] == {
        "app": 1,
        "audio": 1,
        "file": 1,
        "image": 2,
        "sticker": 1,
        "unknown": 1,
        "video": 2,
    }
    rows = _rows(fixture)
    direct = [
        row
        for row in rows
        if json.loads(row["provenance"])["resource_reference"]["peer_id"]
        == "peer-direct"
    ]
    image = next(row for row in direct if row["ordinal"] == 10)
    provenance = json.loads(image["provenance"])
    resource = provenance["resource_reference"]
    assert provenance["server_id"] == str(SERVER)
    assert provenance["backup_id"] == BACKUP
    assert provenance["locator"] == {
        "database": "message_1.sqlite",
        "table": _table("peer-direct"),
        "local_id": 10,
        "row_id": 10,
        "text_origin": "native_resource_metadata",
    }
    assert resource["checksum_md5"] == "d" * 32
    assert resource["byte_size"] == 100
    assert resource["variant"] == "original"
    assert resource["unix_timestamp"] == NOW
    assert image["timestamp_label"] == "11 May 2026 14:23:28"
    assert image["timestamp_value"] == "2026-05-11T14:23:28+08:00"
    assert image["time_sort"] == NOW + 8 * 3600
    file = next(row for row in direct if row["ordinal"] == 12)
    file_reference = json.loads(file["provenance"])["resource_reference"]
    assert file_reference["kind"] == "file"
    assert file_reference["filename"] == "report & data.pdf"
    assert file_reference["byte_size"] == 4321
    assert (
        next(row for row in direct if row["ordinal"] == 13)["text"] == "[App message]"
    )
    assert next(row for row in direct if row["ordinal"] == 11)["sender_id"] == OWNER
    group = next(
        row
        for row in rows
        if json.loads(row["provenance"])["resource_reference"]["peer_id"]
        == "peer-group"
    )
    assert group["sender_id"] is None
    assert group["sender_label"] == "Unknown sender"
    assert "sender_id_unavailable" in json.loads(group["quality_flags"])
    assert source_before == fixture.source.read_bytes()
    assert history_before == fixture.history.read_bytes()
    assert owner_only_problem(resource_index_path(fixture.store)) == ""
    if os.name == "posix":
        assert resource_index_path(fixture.store).stat().st_mode & 0o777 == 0o600


def test_schema_identity_and_attachment_only_conversations_follow_existing_contract(
    resources,
):
    fixture = resources
    build_resource_index(fixture.store, fixture.snapshot)
    with (
        sqlite3.connect(resource_index_path(fixture.store)) as sidecar,
        sqlite3.connect(fixture.history) as main,
    ):
        for table in ("messages", "conversations"):
            assert (
                sidecar.execute(f'PRAGMA table_info("{table}")').fetchall()
                == main.execute(f'PRAGMA table_info("{table}")').fetchall()
            )
        peer = "attachments@chatroom"
        expected = (
            "wechat:"
            + hashlib.sha256(
                (
                    "ios-"
                    + hashlib.md5(OWNER.encode(), usedforsecurity=False).hexdigest()
                    + "\0"
                    + peer
                ).encode()
            ).hexdigest()
        )
        row = sidecar.execute(
            "SELECT id,kind,message_count FROM conversations WHERE peer_id=?", (peer,)
        ).fetchone()
        assert row == (expected, "group", 1)
        info = dict(sidecar.execute("SELECT key,value FROM archive_info"))
        assert info["schema_version"] == "1"
        assert info["messages"] == "9"
        assert set(json.loads(info["history_file_identity"])) == {
            "dev",
            "inode",
            "size",
            "mtime_ns",
        }
        assert len(info["history_partitions_sha256"]) == 64
    first = {row["id"] for row in _rows(fixture)}
    build_resource_index(fixture.store, fixture.snapshot)
    assert {row["id"] for row in _rows(fixture)} == first


@pytest.mark.parametrize("coherent", [True, False])
def test_browse_metadata_needs_exact_account_peer_server_and_time(resources, coherent):
    fixture = resources
    with sqlite3.connect(fixture.owner_root / "FileBrowse/fileInfo.db") as connection:
        connection.execute(
            "INSERT INTO BrowseInfoTable VALUES(?,?,?,?,?,?,?,?,?)",
            (
                SERVER - 3,
                13,
                NOW + 3 if coherent else NOW + 4,
                "document.xlsx",
                "xlsx",
                "f" * 32,
                1234,
                "wxid_explicit_sender",
                "peer-direct",
            ),
        )
    build_resource_index(fixture.store, fixture.snapshot)
    row = next(row for row in _rows(fixture) if row["ordinal"] == 13)
    reference = json.loads(row["provenance"])["resource_reference"]
    assert reference["kind"] == ("file" if coherent else "app")
    assert reference["filename"] == ("document.xlsx" if coherent else "")


def test_existing_positive_server_ids_and_exact_snapshot_locators_are_not_duplicated(
    resources,
):
    fixture = resources
    with sqlite3.connect(fixture.history) as connection:
        conversation = connection.execute(
            "SELECT id FROM conversations WHERE account_key=? AND kind='direct'",
            ("account:" + OWNER,),
        ).fetchone()[0]
        identifier = connection.execute(
            "SELECT id FROM messages WHERE conversation_id=? LIMIT 1", (conversation,)
        ).fetchone()[0]
        connection.execute(
            "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,source_id TEXT,metadata TEXT)"
        )
        connection.execute(
            "INSERT INTO source_snapshots VALUES(1,?,?,?)",
            (
                BACKUP,
                "ios-" + hashlib.md5(OWNER.encode(), usedforsecurity=False).hexdigest(),
                "{}",
            ),
        )
        provenance = {
            "server_id": str(SERVER),
            "source_snapshot": 1,
            "locator": {
                "database": "message_1.sqlite",
                "table": _table("peer-direct"),
                "local_id": 11,
            },
        }
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(provenance), identifier),
        )
    report = build_resource_index(fixture.store, fixture.snapshot)
    assert report["messages"] == 7
    assert report["excluded_existing_records"] == 2
    direct = [
        row
        for row in _rows(fixture)
        if json.loads(row["provenance"])["resource_reference"]["peer_id"]
        == "peer-direct"
    ]
    assert {row["ordinal"] for row in direct}.isdisjoint({10, 11})


def test_source_mutation_aborts_publication_and_preserves_previous_sidecar(resources):
    fixture = resources
    build_resource_index(fixture.store, fixture.snapshot)
    before = resource_index_path(fixture.store).read_bytes()
    from app.core import wechat_resource_index as module

    original = module._table_rows
    changed = False

    def mutate(connection, table):
        nonlocal changed
        rows = original(connection, table)
        if not changed:
            changed = True
            with sqlite3.connect(fixture.source) as writer:
                writer.execute(
                    f'UPDATE "{_table("peer-direct")}" SET CreateTime=CreateTime+1 WHERE MesLocalID=10'
                )
        return rows

    with (
        patch.object(module, "_table_rows", mutate),
        pytest.raises(ValueError, match="input changed"),
    ):
        build_resource_index(fixture.store, fixture.snapshot)
    assert resource_index_path(fixture.store).read_bytes() == before


def test_unverified_requested_account_and_symlink_source_fail_closed(
    resources, tmp_path
):
    fixture = resources
    with pytest.raises(ValueError, match="not verified"):
        build_resource_index(
            fixture.store, fixture.snapshot, account_ids=("wxid_not_an_owner",)
        )
    replacement = tmp_path / "outside.sqlite"
    fixture.source.rename(replacement)
    fixture.source.symlink_to(replacement)
    with pytest.raises(ValueError, match="regular local"):
        build_resource_index(fixture.store, fixture.snapshot)
    assert not resource_index_path(fixture.store).exists()


def test_changed_main_archive_aborts_publication_and_preserves_previous_sidecar(
    resources,
):
    fixture = resources
    build_resource_index(fixture.store, fixture.snapshot)
    previous = resource_index_path(fixture.store).read_bytes()
    from app.core import wechat_resource_index as module

    original = module._table_rows
    changed = False

    def mutate(connection, table):
        nonlocal changed
        rows = original(connection, table)
        if not changed:
            changed = True
            with sqlite3.connect(fixture.history) as writer:
                writer.execute(
                    "INSERT INTO archive_info VALUES('concurrent-reindex-test','changed')"
                )
        return rows

    with (
        patch.object(module, "_table_rows", mutate),
        pytest.raises(ValueError, match="input changed"),
    ):
        build_resource_index(fixture.store, fixture.snapshot)
    assert resource_index_path(fixture.store).read_bytes() == previous


def test_symlinked_database_directory_cannot_cross_native_owner_boundaries(resources):
    fixture = resources
    original = fixture.owner_root / "DB"
    moved = fixture.owner_root.parent / "different-owner-databases"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        build_resource_index(fixture.store, fixture.snapshot)
    assert not resource_index_path(fixture.store).exists()


def test_unsafe_app_xml_and_unknown_direction_do_not_invent_file_or_sender(resources):
    fixture = resources
    with sqlite3.connect(fixture.source) as connection:
        connection.execute(
            f'UPDATE "{_table("peer-direct")}" SET Message=?,WCDB_CT_Message=0,Des=NULL WHERE MesLocalID=13',
            (
                '<!DOCTYPE msg [<!ENTITY x "fake.pdf">]><msg><appmsg><type>6</type><title>&x;</title></appmsg></msg>',
            ),
        )
    build_resource_index(fixture.store, fixture.snapshot)
    row = next(row for row in _rows(fixture) if row["ordinal"] == 13)
    reference = json.loads(row["provenance"])["resource_reference"]
    assert reference["kind"] == "app"
    assert reference["filename"] == ""
    assert row["direction"] == "unknown"
    assert row["sender_id"] is None
    assert "direction_unavailable" in json.loads(row["quality_flags"])


def test_conflicting_original_hashes_do_not_choose_an_arbitrary_file_or_thumbnail(
    resources,
):
    fixture = resources
    with sqlite3.connect(
        fixture.owner_root / "FileIndex/fileIndexInfo.db"
    ) as connection:
        connection.execute(
            "INSERT INTO FileIndexInfoTable VALUES(?,?,?,?,?,?,?,?)",
            ("f" * 32, "&I/different.pic", 101, 10, 10, NOW, 3, "peer-direct"),
        )
    build_resource_index(fixture.store, fixture.snapshot)
    row = next(
        row
        for row in _rows(fixture)
        if row["ordinal"] == 10
        and json.loads(row["provenance"])["resource_reference"]["peer_id"]
        == "peer-direct"
    )
    reference = json.loads(row["provenance"])["resource_reference"]
    assert reference["checksum_md5"] == ""
    assert reference["byte_size"] is None
    assert "resource_checksum_ambiguous" in json.loads(row["quality_flags"])


def test_a_unique_thumbnail_is_labeled_and_cannot_overwrite_browse_metadata(resources):
    fixture = resources
    with sqlite3.connect(
        fixture.owner_root / "FileIndex/fileIndexInfo.db"
    ) as connection:
        connection.execute("DELETE FROM FileIndexInfoTable WHERE indexType=10")
    with sqlite3.connect(fixture.owner_root / "FileBrowse/fileInfo.db") as connection:
        connection.execute(
            "INSERT INTO BrowseInfoTable VALUES(?,?,?,?,?,?,?,?,?)",
            (
                SERVER - 3,
                13,
                NOW + 3,
                "document.pdf",
                "pdf",
                "f" * 32,
                1234,
                "wxid_source_sender",
                "peer-direct",
            ),
        )
    build_resource_index(fixture.store, fixture.snapshot)
    rows = _rows(fixture)
    image = next(
        row
        for row in rows
        if row["ordinal"] == 10
        and json.loads(row["provenance"])["resource_reference"]["peer_id"]
        == "peer-direct"
    )
    reference = json.loads(image["provenance"])["resource_reference"]
    assert reference["checksum_md5"] == "c" * 32
    assert reference["variant"] == "thumbnail"
    file = next(row for row in rows if row["ordinal"] == 13)
    reference = json.loads(file["provenance"])["resource_reference"]
    assert reference["checksum_md5"] == "f" * 32
    assert reference["filename"] == "document.pdf"
    assert reference["byte_size"] == 1234
