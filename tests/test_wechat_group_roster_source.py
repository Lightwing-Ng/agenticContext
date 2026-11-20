"""Native group roster captures keep exact identities and committed source scope.

Code version: v1.1.0-codex.0
"""

from datetime import datetime
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_group_roster_source as roster
from app.core import wechat_mac_refresh_source as native
from tests.test_wechat_identity import _blob, _varint


OWNER = "wxid_roster_viewer"
ROOM = "123456@chatroom"
MEMBERS = (OWNER, "wxid_roster_owner", "wxid_roster_member")


def _room_blob(members=MEMBERS):
    return b"".join(
        _blob(1, _blob(1, member) + (_blob(2, "Room label <b>") if index == 2 else b""))
        for index, member in enumerate(members)
    ) + _varint(3 << 3) + _varint(12345)


@pytest.fixture
def binding(tmp_path):
    directory = OWNER + "_synthetic"
    registry = native.KeyRegistry(key_root=tmp_path / "Keys", native_root=tmp_path / "native")
    database = registry.native_root / directory / "db_storage/contact/contact.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"Synthetic encrypted input, read only by the mocked boundary.")
    row = {
        "account_directory": directory,
        "relative_path": "db_storage/contact/contact.db",
        "database_path": str(database),
        "enc_key": "ab" * 32,
        "verification": "synthetic-authenticated-page-one",
    }
    vault = registry.key_root / "macos-synthetic/verified-db-keys-v1.jsonl"
    vault.parent.mkdir(parents=True)
    vault.write_text(json.dumps(row) + "\n")
    vault.chmod(0o600)
    return registry, vault, database, row


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE chat_room(id INTEGER PRIMARY KEY,username TEXT,owner TEXT,ext_buffer BLOB);"
        "CREATE TABLE chatroom_member(room_id INTEGER,member_id INTEGER);"
        "CREATE TABLE name2id(username TEXT PRIMARY KEY);"
        "CREATE TABLE contact(id INTEGER PRIMARY KEY,username TEXT,nick_name TEXT,"
        "is_in_chat_room INTEGER,delete_flag INTEGER);"
    )
    for index, member in enumerate(MEMBERS, 1):
        connection.execute("INSERT INTO name2id(rowid,username) VALUES(?,?)", (index, member))
        connection.execute("INSERT INTO contact VALUES(?,?,?,0,0)", (index, member, "Nickname"))
    for index in reversed(range(1, len(MEMBERS) + 1)):
        connection.execute("INSERT INTO chatroom_member VALUES(10,?)", (index,))
    connection.execute("INSERT INTO name2id(rowid,username) VALUES(10,?)", (ROOM,))
    connection.execute("INSERT INTO contact VALUES(10,?,'Group',1,0)", (ROOM,))
    connection.execute("INSERT INTO chat_room VALUES(10,?,?,?)", (ROOM, MEMBERS[1], _room_blob()))
    result = native.AuthenticatedDatabase(
        connection,
        {
            "source_id": "synthetic-contact-source",
            "snapshot_id": "synthetic-committed-snapshot",
            "main_sha256": "a" * 64,
            "wal_sha256": "b" * 64,
            "integrity_check": "ok",
            "authenticated_pages": True,
            "page_count": 1,
            "authenticated_page_count": 1,
        },
        OWNER + "_synthetic",
        False,
        "none",
    )
    yield result
    connection.close()


def _capture(monkeypatch, binding, database):
    monkeypatch.setattr(native, "read_database", lambda _reference, *, check_cancel=None: database)
    return roster.capture_group_roster(OWNER, ROOM, registry=binding[0])


def test_contact_availability_is_metadata_only_and_keeps_message_registry_scoped(binding, monkeypatch):
    registry, _vault, _path, _row = binding
    monkeypatch.setattr(native, "read_database", lambda *_args: pytest.fail("metadata decrypted data"))
    assert roster.roster_source_available(OWNER, registry=registry)
    assert not roster.roster_source_available("wxid_other_account", registry=registry)
    assert not roster.roster_source_available("../outside", registry=registry)
    assert registry.references(OWNER) == []
    reference = roster._contact_reference(OWNER, registry, load_key=False)
    assert reference.key is None
    assert "ab" * 32 not in repr(reference)


def test_capture_cross_checks_members_and_preserves_native_order(binding, database, monkeypatch):
    original = binding[2].read_bytes()
    result = _capture(monkeypatch, binding, database)
    assert result["account_id"] == OWNER and result["peer_id"] == ROOM
    assert datetime.fromisoformat(result["observed_at"]).utcoffset() is not None
    assert result["complete"] and result["member_count"] == 3
    members = {member["username"]: member["display_name"] for member in result["members"]}
    assert members == {MEMBERS[0]: "Nickname", MEMBERS[1]: "Nickname", MEMBERS[2]: "Room label <b>"}
    assert tuple(members) == MEMBERS
    encoded_order = json.dumps(MEMBERS, ensure_ascii=False, separators=(",", ":")).encode()
    encoded_set = json.dumps(sorted(MEMBERS), ensure_ascii=False, separators=(",", ":")).encode()
    assert result["source"]["member_order_sha256"] == hashlib.sha256(encoded_order).hexdigest()
    assert result["source"]["membership_sha256"] == hashlib.sha256(encoded_set).hexdigest()
    assert result["source"]["adapter_version"] == "v1.1.0-codex.0"
    assert result["source"]["owner_id"] == MEMBERS[1]
    assert result["source"]["completeness_scope"] == "committed_local_roster"
    assert result["source"]["wal_tail_status"] == "none"
    assert result["source"]["wal_tail_complete"] is True
    assert "enc_key" not in json.dumps(result)
    assert binding[2].read_bytes() == original
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.connection.execute("SELECT 1")


@pytest.mark.parametrize("tail", ["zero_preallocation", "stale_salt_tail", "uncommitted_frames"])
def test_committed_local_membership_preserves_partial_wal_status(binding, database, monkeypatch, tail):
    database.partial = True
    database.wal_tail_status = tail
    result = _capture(monkeypatch, binding, database)
    assert result["complete"] is True
    assert result["source"]["wal_tail_complete"] is False
    assert result["source"]["wal_tail_status"] == tail


@pytest.mark.parametrize("tail", ["incomplete_frame_header", "incomplete_frame", "unknown"])
def test_incomplete_contact_wal_tail_refuses_a_roster(binding, database, monkeypatch, tail):
    database.partial = True
    database.wal_tail_status = tail
    with pytest.raises(native.RefreshError, match="WAL tail"):
        _capture(monkeypatch, binding, database)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.connection.execute("SELECT 1")


@pytest.mark.parametrize("change", [
    "missing_lookup", "conflicting_lookup", "duplicate_edge", "ext_member_missing",
    "ext_member_duplicate", "ext_member_corrupt", "missing_self", "missing_owner",
    "inactive_room", "schema",
])
def test_incomplete_or_ambiguous_member_sets_are_rejected(binding, database, monkeypatch, change):
    c = database.connection
    if change == "missing_lookup":
        c.execute("DELETE FROM name2id WHERE username=?", (MEMBERS[2],))
    elif change == "conflicting_lookup":
        c.execute("UPDATE contact SET username='wxid_conflicting' WHERE id=3")
    elif change == "duplicate_edge":
        c.execute("INSERT INTO chatroom_member VALUES(10,3)")
    elif change == "ext_member_missing":
        c.execute("UPDATE chat_room SET ext_buffer=?", (_room_blob(MEMBERS[:-1]),))
    elif change == "ext_member_duplicate":
        c.execute("UPDATE chat_room SET ext_buffer=?", (_room_blob((*MEMBERS, MEMBERS[-1])),))
    elif change == "ext_member_corrupt":
        c.execute("UPDATE chat_room SET ext_buffer=?", (_room_blob()[:-1],))
    elif change == "missing_self":
        c.execute("DELETE FROM chatroom_member WHERE member_id=1")
        c.execute("UPDATE chat_room SET ext_buffer=?", (_room_blob(MEMBERS[1:]),))
    elif change == "missing_owner":
        c.execute("UPDATE chat_room SET owner='wxid_absent_owner'")
    elif change == "inactive_room":
        c.execute("UPDATE contact SET is_in_chat_room=0 WHERE username=?", (ROOM,))
    else:
        c.execute("DROP TABLE chatroom_member")
    with pytest.raises(native.RefreshError):
        _capture(monkeypatch, binding, database)


@pytest.mark.parametrize("change", ["wrong_path", "invalid_key", "conflicting_key", "second_directory"])
def test_contact_key_cannot_cross_source_bindings(binding, change):
    registry, vault, _path, row = binding
    rows = [row]
    if change == "wrong_path":
        row["database_path"] += ".different"
    elif change == "invalid_key":
        row["enc_key"] = "invalid"
    elif change == "conflicting_key":
        rows.append(dict(row, enc_key="cd" * 32))
    else:
        directory = OWNER + "_another"
        another = registry.native_root / directory / row["relative_path"]
        another.parent.mkdir(parents=True)
        another.write_bytes(b"Another configured encrypted source.")
        rows.append(dict(row, account_directory=directory, database_path=str(another)))
    vault.write_text("\n".join(map(json.dumps, rows)) + "\n")
    with pytest.raises(native.RefreshError):
        roster._contact_reference(OWNER, registry, load_key=True)
