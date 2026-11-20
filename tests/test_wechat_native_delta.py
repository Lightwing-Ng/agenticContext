"""Exercise exact native source changes independently of clocks and row order.

Code version: v1.0.0-codex.0
"""

import sqlite3
import hashlib

import pytest

from app.core.wechat_native_delta import iter_native_row_deltas
from app.core.wechat_native_delta import project_native_text, prove_preserved_tables


TABLE = "Chat_" + "a" * 32


def database(rows=(), *, key="MesLocalID INTEGER PRIMARY KEY", extra=""):
    connection = sqlite3.connect(":memory:")
    connection.execute(
        f'CREATE TABLE "{TABLE}" ({key},CreateTime INTEGER,Message BLOB{extra})'
    )
    connection.executemany(f'INSERT INTO "{TABLE}" VALUES(?,?,?)', rows)
    return connection


def test_late_arrival_and_changed_body_are_found_without_time_cutoff():
    old = database([(1, 100, "first"), (2, 200, "retained"), (4, 400, b"opaque")])
    new = database([(1, 100, "edited"), (2, 200, "retained"), (3, 1, "late old clock")])
    try:
        result = list(iter_native_row_deltas(old, new, TABLE))
        assert [(r.kind, r.key) for r in result] == [
            ("changed", (1,)), ("inserted", (3,)), ("deleted", (4,))
        ]
        assert result[0].before["Message"] == "first"
        assert result[0].after["Message"] == "edited"
        assert result[1].after["CreateTime"] == 1
        assert result[2].before["Message"] == b"opaque"
        assert old.execute(f'SELECT COUNT(*) FROM "{TABLE}"').fetchone() == (3,)
    finally:
        old.close()
        new.close()


def test_internal_rowid_change_does_not_invent_a_new_message():
    old = database([("key", 100, "same")], key="MesLocalID TEXT PRIMARY KEY")
    new = database([("key", 100, "same")], key="MesLocalID TEXT PRIMARY KEY")
    new.execute(f'UPDATE "{TABLE}" SET rowid=27')
    try:
        assert list(iter_native_row_deltas(old, new, TABLE)) == []
    finally:
        old.close()
        new.close()


@pytest.mark.parametrize("before,after", [("literal", b"literal"), (1, 1.0)])
def test_storage_class_changes_are_not_silently_equal(before, after):
    old, new = database([(1, 100, before)]), database([(1, 100, after)])
    try:
        assert [row.kind for row in iter_native_row_deltas(old, new, TABLE)] == ["changed"]
    finally:
        old.close()
        new.close()


def test_first_table_import_preserves_literal_whitespace_and_opaque_bytes():
    new = database([(1, 0, "  • literal\n\n"), (2, 0, b"\xff\x00")])
    try:
        result = list(iter_native_row_deltas(None, new, TABLE))
        assert [row.after["Message"] for row in result] == ["  • literal\n\n", b"\xff\x00"]
    finally:
        new.close()


@pytest.mark.parametrize("invalid", ["missing_key", "null_key", "schema_change", "injected_table"])
def test_unsupported_source_layout_fails_closed(invalid):
    old = database([(1, 1, "same")])
    new = database([(1, 1, "same")])
    table = TABLE
    try:
        if invalid == "missing_key":
            new.execute(f'DROP TABLE "{TABLE}"')
            new.execute(f'CREATE TABLE "{TABLE}" (MesLocalID INTEGER,CreateTime INTEGER,Message BLOB)')
        elif invalid == "null_key":
            new.execute(f'DROP TABLE "{TABLE}"')
            new.execute(f'CREATE TABLE "{TABLE}" (MesLocalID TEXT PRIMARY KEY,CreateTime INTEGER,Message BLOB)')
            new.execute(f'INSERT INTO "{TABLE}" VALUES(NULL,1,?)', ("same",))
        elif invalid == "schema_change":
            new.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN Extra INTEGER')
        else:
            table += '"; DROP TABLE protected; --'
        with pytest.raises(ValueError):
            list(iter_native_row_deltas(old, new, table))
    finally:
        old.close()
        new.close()


def test_projection_retains_literal_source_body_and_independent_native_clock():
    owner, peer = "wxid_fixture_owner", "wxid_fixture_peer"
    source = {"source_account_id": owner, "source_id": "ios-" + hashlib.md5(owner.encode()).hexdigest(),
              "account_hash_verified": True, "snapshot_id": "native-snapshot"}
    table = "Chat_" + hashlib.md5(peer.encode()).hexdigest()
    row = {"source_rowid": 7, "MesLocalID": 7, "MesSvrID": 1234, "Type": 1,
           "CreateTime": 1, "Des": 1, "Message": " • literal\n\n", "WCDB_CT_Message": 0}
    spec = {"path": "Documents/native/DB/message_1.sqlite", "sha256": "a" * 64, "file_id": "b" * 40}
    result = project_native_text(row, source=source, database=spec, table=table, peer=peer, kind="direct")
    assert result["text"] == row["Message"]
    assert result["sender_id"] == peer
    assert result["sent_at_utc_iso"] is None and result["timeline_timestamp_usable"] is False
    assert result["source_locator"]["native_message_sha256"] == hashlib.sha256(row["Message"].encode()).hexdigest()
    with pytest.raises(ValueError):
        project_native_text(row, source=source, database=spec, table=table, peer=owner, kind="direct")


def test_preservation_joins_logical_key_after_replace_and_rejects_real_change(tmp_path):
    before, after = tmp_path / "before.sqlite3", tmp_path / "after.sqlite3"
    for path in (before, after):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE evidence(id TEXT PRIMARY KEY,body BLOB)")
            connection.execute("INSERT INTO evidence VALUES('retained',?)", (b"source",))
    with sqlite3.connect(after) as connection:
        connection.execute("INSERT OR REPLACE INTO evidence VALUES('retained',?)", (b"source",))
        connection.execute("INSERT INTO evidence VALUES('new',?)", (b"new",))
    assert prove_preserved_tables(before, after)["evidence"]["old_rows_verified"] == 1
    with sqlite3.connect(after) as connection:
        connection.execute("UPDATE evidence SET body=? WHERE id='retained'", (b"changed",))
    with pytest.raises(ValueError, match="retained row"):
        prove_preserved_tables(before, after)
