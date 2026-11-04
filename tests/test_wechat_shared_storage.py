"""Share exact conversation payloads without changing any source observation.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from app.core.wechat_archive import _SCHEMA
from app.core.wechat_shared_storage import compact_shared_group_bodies


_INSERT = "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
_ROOM = "synthetic-shared-room@chatroom"


def _identifier(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _conversation(connection, owner, *, peer=_ROOM, verified=True, kind="group"):
    partition = "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    account = "account:" + owner
    conversation = "wechat:" + _identifier(partition + "\0" + peer)
    connection.execute(
        "INSERT OR IGNORE INTO partitions VALUES(?,?,?,?,?,?,?,?)",
        (
            partition,
            account,
            owner,
            "Synthetic account",
            "Verified archive ID" if verified else "Source account unconfirmed",
            "iPhone archive",
            "Synthetic native backup",
            "Synthetic native text payload",
        ),
    )
    connection.execute(
        "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            conversation,
            partition,
            account,
            peer,
            "Synthetic shared room",
            kind,
            0,
            0.0,
            0.0,
            "",
            "",
            "",
        ),
    )
    return conversation


def _message(connection, conversation, number, *, text=None, search=None, direction="received"):
    text = text if text is not None else f"Exact message {number}: 原始文字"
    search = search if search is not None else text.casefold()
    owner = connection.execute(
        "SELECT p.account_id FROM conversations c JOIN partitions p ON p.id=c.partition_id "
        "WHERE c.id=?",
        (conversation,),
    ).fetchone()[0]
    provenance = json.dumps(
        {
            "record_id": f"source-local-{number}",
            "server_id": str(number),
            "message_type": 1,
            "text_extraction_kind": "plain_text",
            "locator": {"database": owner + ".sqlite", "row": number},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    identifier = _identifier(conversation + ":" + str(number))
    connection.execute(
        _INSERT,
        (
            identifier,
            conversation,
            number,
            "wxid_synthetic_author",
            "Synthetic sender",
            direction,
            text,
            float(number),
            f"2026-11-03T00:00:{number:02}+08:00",
            f"3 Nov 2026 00:00:{number:02}",
            "UTC+08:00",
            provenance,
            "[]",
            search,
        ),
    )
    return identifier


def _rows(connection):
    return connection.execute("SELECT * FROM messages ORDER BY id").fetchall()


def _payload_bytes(rows):
    return sum(len(row[6].encode()) + len(row[13].encode()) for row in rows)


@pytest.fixture
def shared_archive():
    connection = sqlite3.connect(":memory:")
    connection.executescript(_SCHEMA)
    first = _conversation(connection, "wxid_synthetic_first")
    second = _conversation(connection, "wxid_synthetic_second")
    identifiers = {}
    for number in (1, 2, 4):
        identifiers[(first, number)] = _message(connection, first, number)
    for number in (2, 3):
        identifiers[(second, number)] = _message(connection, second, number)
    connection.commit()
    try:
        yield connection, [first, second], identifiers
    finally:
        connection.close()


@pytest.fixture
def private_archive():
    connection = sqlite3.connect(":memory:")
    connection.executescript(_SCHEMA)
    owners = ("wxid_synthetic_private_first", "wxid_synthetic_private_second")
    first = _conversation(connection, owners[0], peer=owners[1], kind="direct")
    second = _conversation(connection, owners[1], peer=owners[0], kind="direct")
    identifiers = {}
    for number in (1, 2, 4):
        identifiers[(first, number)] = _message(connection, first, number, direction="sent")
    for number in (2, 3):
        identifiers[(second, number)] = _message(connection, second, number)
    connection.execute(
        "UPDATE messages SET sender_id=?,sender_label='First account local label' "
        "WHERE conversation_id=?",
        (owners[0], first),
    )
    connection.execute(
        "UPDATE messages SET sender_id=?,sender_label='Second account contact remark' "
        "WHERE conversation_id=?",
        (owners[0], second),
    )
    connection.commit()
    try:
        yield connection, [first, second], identifiers
    finally:
        connection.close()


@pytest.fixture
def private_batch_archive(shared_archive):
    connection, group_conversations, _ = shared_archive
    common = connection.execute(
        "SELECT text FROM messages WHERE conversation_id=? AND ordinal=2", (group_conversations[0],)
    ).fetchone()[0]
    scopes = []
    for pair_number in range(2):
        owners = (
            f"wxid_private_batch_{pair_number}_first",
            f"wxid_private_batch_{pair_number}_second",
        )
        first = _conversation(connection, owners[0], peer=owners[1], kind="direct")
        second = _conversation(connection, owners[1], peer=owners[0], kind="direct")
        _message(connection, first, 2, text=common, direction="sent")
        _message(connection, second, 2, text=common)
        _message(connection, first, 10 + pair_number * 2)
        _message(connection, second, 11 + pair_number * 2)
        scopes.append([first, second])
    connection.commit()
    return connection, scopes, group_conversations, common


@pytest.mark.parametrize("pool_already_exists", [False, True])
def test_batch_counts_shared_pool_bytes_once_and_reports_only_new_marginal_savings(
    private_batch_archive, pool_already_exists
):
    from app.core.wechat_shared_storage import (
        analyze_shared_conversation_scopes,
        compact_shared_conversation_scopes,
    )

    connection, scopes, group_conversations, common = private_batch_archive
    if pool_already_exists:
        compact_shared_group_bodies(connection, group_conversations)
    before = _rows(connection)
    body_bytes = len(common.encode()) + len(common.casefold().encode())
    preview = analyze_shared_conversation_scopes(connection, scopes)
    report = compact_shared_conversation_scopes(connection, scopes)

    assert report == preview
    assert _rows(connection) == before
    assert report["observations"] == 8
    assert len(report["scopes"]) == 2
    assert all(scope["payload_bytes_saved"] == body_bytes for scope in report["scopes"])
    assert report["payload_bytes_saved"] == 2 * body_bytes
    expected_incremental = (4 if pool_already_exists else 3) * body_bytes
    assert report["incremental_payload_bytes_saved"] == expected_incremental
    assert report["stored_payload_bytes_before"] - report["stored_payload_bytes_after"] == expected_incremental
    assert connection.execute("SELECT COUNT(*) FROM shared_message_bodies").fetchone()[0] == 1
    repeated = compact_shared_conversation_scopes(connection, scopes)
    assert repeated["observations_to_rewrite"] == 0
    assert repeated["incremental_payload_bytes_saved"] == 0
    assert repeated["payload_bytes_saved"] == 2 * body_bytes
    assert repeated["stored_payload_bytes_before"] == repeated["stored_payload_bytes_after"]
    assert _rows(connection) == before


def test_batch_rejects_overlapping_conversation_scopes_before_any_mutation(private_batch_archive):
    from app.core.wechat_shared_storage import (
        analyze_shared_conversation_scopes,
        compact_shared_conversation_scopes,
    )

    connection, scopes, _group_conversations, _common = private_batch_archive
    overlapping = [scopes[0], list(reversed(scopes[0]))]
    before = list(connection.iterdump())
    for operation in (analyze_shared_conversation_scopes, compact_shared_conversation_scopes):
        with pytest.raises(ValueError):
            operation(connection, overlapping)
        assert list(connection.iterdump()) == before


def test_batch_observation_budget_applies_to_the_combined_private_scope(
    private_batch_archive, monkeypatch
):
    from app.core import wechat_shared_storage as storage

    connection, scopes, _group_conversations, _common = private_batch_archive
    for _first, second in scopes:
        connection.execute(
            "DELETE FROM messages WHERE conversation_id=? AND ordinal>=10", (second,)
        )
    monkeypatch.setattr(storage, "_MAX_OBSERVATIONS", 5)
    assert all(
        storage.analyze_shared_conversation_bodies(connection, scope)["observations"] == 3
        for scope in scopes
    )
    before = list(connection.iterdump())
    for operation in (
        storage.analyze_shared_conversation_scopes, storage.compact_shared_conversation_scopes
    ):
        with pytest.raises(ValueError):
            operation(connection, scopes)
        assert list(connection.iterdump()) == before


def test_reciprocal_private_chat_shares_bodies_and_preserves_both_account_views(private_archive):
    from app.core.wechat_shared_storage import (
        analyze_shared_conversation_bodies,
        compact_shared_conversation_bodies,
    )

    connection, conversations, identifiers = private_archive
    before = _rows(connection)
    metadata = connection.execute("SELECT * FROM conversations ORDER BY id").fetchall()
    preview = analyze_shared_conversation_bodies(connection, conversations)
    report = compact_shared_conversation_bodies(connection, conversations)

    assert report == preview
    assert report["observations"] == 5
    assert report["shared_payloads"] == 1
    assert report["shared_observations"] == 2
    assert report["payload_bytes_saved"] > 0
    assert _rows(connection) == before
    assert connection.execute("SELECT * FROM conversations ORDER BY id").fetchall() == metadata
    assert connection.execute(
        "SELECT conversation_id,COUNT(*) FROM messages GROUP BY conversation_id ORDER BY conversation_id"
    ).fetchall() == sorted([(conversations[0], 3), (conversations[1], 2)])
    source_views = connection.execute(
        "SELECT id,direction,sender_label,provenance FROM messages WHERE id IN (?,?) ORDER BY id",
        (identifiers[(conversations[0], 2)], identifiers[(conversations[1], 2)]),
    ).fetchall()
    assert {row[1] for row in source_views} == {"sent", "received"}
    assert len({row[2] for row in source_views}) == 2
    assert len({row[3] for row in source_views}) == 2


def test_private_chat_keeps_conflicts_repeats_search_suffixes_and_late_gaps(private_archive):
    from app.core.wechat_shared_storage import compact_shared_conversation_bodies

    connection, conversations, _ = private_archive
    first, second = conversations
    _message(connection, first, 7, text="First private source variant")
    _message(connection, second, 7, text="Second private source variant")
    text = "Repeated exact private text"
    _message(connection, first, 8, text=text, search=text.casefold() + "\nfirst private alias")
    _message(connection, second, 9, text=text, search=text.casefold() + "\nsecond private alias")
    before = _rows(connection)
    report = compact_shared_conversation_bodies(connection, conversations)

    assert _rows(connection) == before
    assert report["observations"] == 9
    assert report["shared_payloads"] == 2
    assert report["shared_observations"] == 4
    assert connection.execute(
        "SELECT conversation_id FROM messages WHERE instr(search_text,'first private alias')>0"
    ).fetchall() == [(first,)]
    assert connection.execute(
        "SELECT conversation_id FROM messages WHERE instr(search_text,'second private alias')>0"
    ).fetchall() == [(second,)]
    repeated = compact_shared_conversation_bodies(connection, conversations)
    assert repeated["observations_to_rewrite"] == 0
    _message(connection, second, 4)
    after_late_import = _rows(connection)
    late = compact_shared_conversation_bodies(connection, conversations)
    assert _rows(connection) == after_late_import
    assert late["observations"] == 10
    assert late["shared_payloads"] == 3
    assert late["observations_to_rewrite"] == 2


@pytest.mark.parametrize(
    "scope_problem",
    ["one-way", "extra-account", "mixed-kind", "self-chat", "unverified", "spoof-owner", "spoof-partition"],
)
def test_private_scope_requires_exact_reciprocal_verified_accounts(private_archive, scope_problem):
    from app.core.wechat_shared_storage import compact_shared_conversation_bodies

    connection, conversations, _ = private_archive
    if scope_problem == "one-way":
        connection.execute(
            "UPDATE conversations SET peer_id='wxid_unrelated_peer' WHERE id=?", (conversations[1],)
        )
    elif scope_problem == "extra-account":
        third = _conversation(
            connection, "wxid_synthetic_private_third", peer="wxid_synthetic_private_first", kind="direct"
        )
        conversations = [*conversations, third]
    elif scope_problem == "mixed-kind":
        connection.execute("UPDATE conversations SET kind='group' WHERE id=?", (conversations[1],))
    elif scope_problem == "self-chat":
        connection.execute(
            "UPDATE conversations SET peer_id='wxid_synthetic_private_first' WHERE id=?",
            (conversations[0],),
        )
    elif scope_problem == "unverified":
        connection.execute(
            "UPDATE partitions SET identity_status='Source account unconfirmed' "
            "WHERE id=(SELECT partition_id FROM conversations WHERE id=?)",
            (conversations[1],),
        )
    elif scope_problem == "spoof-owner":
        connection.execute(
            "UPDATE partitions SET account_id='wxid_spoofed_private_owner' "
            "WHERE id=(SELECT partition_id FROM conversations WHERE id=?)",
            (conversations[1],),
        )
    else:
        connection.execute(
            "UPDATE conversations SET partition_id='ios-unverified-partition' WHERE id=?",
            (conversations[1],),
        )
    before = list(connection.iterdump())
    with pytest.raises(ValueError):
        compact_shared_conversation_bodies(connection, conversations)
    assert list(connection.iterdump()) == before


def test_partial_overlap_preserves_all_observations_and_source_bytes(shared_archive):
    connection, conversations, _ = shared_archive
    before = _rows(connection)
    metadata = connection.execute("SELECT * FROM conversations ORDER BY id").fetchall()
    report = compact_shared_group_bodies(connection, conversations)

    assert _rows(connection) == before
    assert connection.execute("SELECT * FROM conversations ORDER BY id").fetchall() == metadata
    assert report["observations"] == 5
    assert report["shared_payloads"] == 1
    assert report["shared_observations"] == 2
    assert report["observations_to_rewrite"] == 2
    saved = _payload_bytes([next(row for row in before if row[2] == 2)])
    assert report["payload_bytes_before"] == _payload_bytes(before)
    assert report["payload_bytes_saved"] == saved
    assert report["payload_bytes_after"] == _payload_bytes(before) - saved
    assert connection.execute("SELECT COUNT(*) FROM shared_message_bodies").fetchone()[0] == 1
    assert connection.execute(
        "SELECT conversation_id,COUNT(*) FROM messages GROUP BY conversation_id ORDER BY conversation_id"
    ).fetchall() == sorted([(conversations[0], 3), (conversations[1], 2)])


def test_shared_projection_keeps_search_and_pagination_results(shared_archive):
    connection, conversations, _ = shared_archive
    query = (
        "SELECT id,text,search_text,provenance FROM messages "
        "WHERE conversation_id=? AND instr(search_text,?)>0 "
        "ORDER BY time_sort DESC,ordinal DESC LIMIT ? OFFSET ?"
    )
    arguments = (conversations[0], "原始文字", 2, 1)
    expected = connection.execute(query, arguments).fetchall()
    compact_shared_group_bodies(connection, conversations)
    assert connection.execute(query, arguments).fetchall() == expected
    assert len(connection.execute("PRAGMA table_info(messages)").fetchall()) == 14
    assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_published_format_reads_from_plain_readonly_and_attached_connections(shared_archive, tmp_path):
    connection, conversations, _ = shared_archive
    before = _rows(connection)
    compact_shared_group_bodies(connection, conversations)
    connection.commit()
    database = tmp_path / "shared-history.sqlite3"
    with sqlite3.connect(database) as output:
        connection.backup(output)

    with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as readonly:
        assert _rows(readonly) == before
        assert readonly.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with sqlite3.connect(":memory:", uri=True) as reader:
        reader.execute("ATTACH DATABASE ? AS archive", (database.as_uri() + "?mode=ro",))
        assert reader.execute("SELECT * FROM archive.messages ORDER BY id").fetchall() == before
        assert reader.execute(
            "SELECT m.id,p.account_id FROM archive.messages m "
            "JOIN archive.conversations c ON c.id=m.conversation_id "
            "JOIN archive.partitions p ON p.id=c.partition_id ORDER BY m.id"
        ).fetchall() == connection.execute(
            "SELECT m.id,p.account_id FROM messages m "
            "JOIN conversations c ON c.id=m.conversation_id "
            "JOIN partitions p ON p.id=c.partition_id ORDER BY m.id"
        ).fetchall()


def test_equal_payloads_do_not_merge_distinct_events_or_account_directions(shared_archive):
    connection, conversations, _ = shared_archive
    first = _message(connection, conversations[0], 5, text="Repeated unchanged text", direction="sent")
    second = _message(connection, conversations[1], 6, text="Repeated unchanged text")
    before = _rows(connection)
    report = compact_shared_group_bodies(connection, conversations)

    assert _rows(connection) == before
    assert report["observations"] == 7
    assert report["shared_payloads"] == 2
    assert report["shared_observations"] == 4
    assert connection.execute(
        "SELECT id,direction,time_sort FROM messages WHERE id IN (?,?) ORDER BY id",
        (first, second),
    ).fetchall() == sorted([(first, "sent", 5.0), (second, "received", 6.0)])


def test_conflicting_text_or_search_material_is_preserved(shared_archive):
    connection, conversations, _ = shared_archive
    _message(connection, conversations[0], 7, text="Original conflicting text")
    _message(connection, conversations[1], 7, text="Another conflicting text")
    _message(connection, conversations[0], 8, text="Same body", search="first local alias")
    _message(connection, conversations[1], 8, text="Same body", search="second local alias")
    before = _rows(connection)
    report = compact_shared_group_bodies(connection, conversations)

    assert _rows(connection) == before
    assert report["observations"] == 9
    assert report["shared_payloads"] == 1
    assert report["shared_observations"] == 2


def test_account_specific_search_suffixes_share_only_the_body_prefix(shared_archive):
    connection, conversations, _ = shared_archive
    text = "A shared message with account-specific aliases"
    first_search = text.casefold() + "\nFirst local nickname\n" + _ROOM
    second_search = text.casefold() + "\nSecond local nickname\n" + _ROOM
    first = _message(connection, conversations[0], 7, text=text, search=first_search)
    second = _message(connection, conversations[1], 7, text=text, search=second_search)
    before = _rows(connection)
    report = compact_shared_group_bodies(connection, conversations)

    assert _rows(connection) == before
    assert report["shared_payloads"] == 2
    assert report["shared_observations"] == 4
    assert connection.execute(
        "SELECT id FROM messages WHERE instr(search_text,'First local nickname')>0"
    ).fetchall() == [(first,)]
    assert connection.execute(
        "SELECT id FROM messages WHERE instr(search_text,'Second local nickname')>0"
    ).fetchall() == [(second,)]


@pytest.mark.parametrize("scope_problem", ["direct", "unverified", "different-room"])
def test_invalid_scope_is_rejected_before_mutation(shared_archive, scope_problem):
    connection, conversations, _ = shared_archive
    if scope_problem == "direct":
        connection.execute("UPDATE conversations SET kind='direct' WHERE id=?", (conversations[1],))
    elif scope_problem == "unverified":
        connection.execute(
            "UPDATE partitions SET identity_status='Source account unconfirmed' "
            "WHERE id=(SELECT partition_id FROM conversations WHERE id=?)",
            (conversations[1],),
        )
    else:
        connection.execute(
            "UPDATE conversations SET peer_id='another-room@chatroom' WHERE id=?", (conversations[1],)
        )
    before = list(connection.iterdump())
    with pytest.raises(ValueError):
        compact_shared_group_bodies(connection, conversations)
    assert list(connection.iterdump()) == before


def test_no_cross_account_payload_match_does_not_install_shared_format(shared_archive):
    connection, conversations, identifiers = shared_archive
    connection.execute(
        "UPDATE messages SET text='A different exact source body' WHERE id=?",
        (identifiers[(conversations[1], 2)],),
    )
    before = list(connection.iterdump())
    report = compact_shared_group_bodies(connection, conversations)

    assert report["shared_payloads"] == report["shared_observations"] == 0
    assert report["payload_bytes_saved"] == 0
    assert list(connection.iterdump()) == before


def test_rerun_is_idempotent_and_late_observation_can_share_existing_body(shared_archive):
    connection, conversations, _ = shared_archive
    first = compact_shared_group_bodies(connection, conversations)
    assert first["observations_to_rewrite"] == 2
    before = list(connection.iterdump())
    repeated = compact_shared_group_bodies(connection, conversations)
    assert repeated["observations_to_rewrite"] == 0
    assert list(connection.iterdump()) == before

    late = _message(connection, conversations[1], 4)
    before_rows = _rows(connection)
    report = compact_shared_group_bodies(connection, conversations)
    assert _rows(connection) == before_rows
    assert report["observations"] == 6
    assert report["shared_payloads"] == 2
    assert report["shared_observations"] == 4
    assert report["observations_to_rewrite"] == 2
    assert connection.execute("SELECT text FROM messages WHERE id=?", (late,)).fetchone()[0].startswith(
        "Exact message 4:"
    )


def test_legacy_writers_detach_one_observation_without_changing_other_accounts(shared_archive):
    connection, conversations, identifiers = shared_archive
    compact_shared_group_bodies(connection, conversations)
    changed = identifiers[(conversations[0], 2)]
    retained = identifiers[(conversations[1], 2)]
    original = connection.execute("SELECT * FROM messages WHERE id=?", (retained,)).fetchone()
    connection.execute(
        "UPDATE messages SET text=?,search_text=? WHERE id=?",
        ("A new exact source version", "a new exact source version", changed),
    )
    assert connection.execute("SELECT * FROM messages WHERE id=?", (retained,)).fetchone() == original
    assert connection.execute("SELECT text FROM messages WHERE id=?", (changed,)).fetchone()[0] == (
        "A new exact source version"
    )
    inserted = _message(connection, conversations[0], 9)
    assert connection.execute("SELECT COUNT(*) FROM messages WHERE id=?", (inserted,)).fetchone()[0] == 1
    connection.execute("DELETE FROM messages WHERE id=?", (changed,))
    assert connection.execute("SELECT * FROM messages WHERE id=?", (retained,)).fetchone() == original
    assert connection.execute("SELECT COUNT(*) FROM messages WHERE id=?", (changed,)).fetchone()[0] == 0


def test_pool_is_immutable_and_dangling_references_are_rejected(shared_archive):
    connection, conversations, identifiers = shared_archive
    compact_shared_group_bodies(connection, conversations)
    before = _rows(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE shared_message_bodies SET text='Changed shared evidence'")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM shared_message_bodies")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE message_storage SET content_ref=999999999 WHERE id=?",
            (identifiers[(conversations[0], 1)],),
        )
    assert _rows(connection) == before


def test_replace_cannot_overwrite_an_existing_shared_body_identifier(shared_archive):
    connection, conversations, _ = shared_archive
    compact_shared_group_bodies(connection, conversations)
    before = _rows(connection)
    original = connection.execute("SELECT id,text,search_text FROM shared_message_bodies").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT OR REPLACE INTO shared_message_bodies(id,text,search_text) VALUES(?,?,?)",
            (original[0], "A forbidden replacement body", "a forbidden replacement body"),
        )
    assert connection.execute("SELECT id,text,search_text FROM shared_message_bodies").fetchone() == original
    assert _rows(connection) == before


@pytest.mark.parametrize("writer", ["native", "export"])
def test_append_only_writer_guards_support_shared_storage(shared_archive, writer):
    from app.core.wechat_export_merge import _install_guard
    from app.core.wechat_native_restore import _install_guards
    from app.core.wechat_reconcile import _ADDITIVE_SCHEMA

    connection, conversations, _ = shared_archive
    connection.executescript(_ADDITIVE_SCHEMA)
    compact_shared_group_bodies(connection, conversations)
    before = _rows(connection)
    if writer == "native":
        scope = _install_guards(connection)
    else:
        scope = _install_guard(connection)
    try:
        inserted = _message(connection, conversations[0], 9)
        if writer == "native":
            assert connection.execute("SELECT id FROM restore_added_messages").fetchall() == [(inserted,)]
        else:
            scope["new_message_read"] = True
        assert all(row in _rows(connection) for row in before)
        assert len(_rows(connection)) == len(before) + 1
        for statement in (
            "UPDATE messages SET text='Forbidden old source rewrite'",
            "DELETE FROM messages",
            "UPDATE message_storage SET text='Forbidden physical rewrite'",
            "DELETE FROM message_storage",
            "INSERT INTO shared_message_bodies(text,search_text) VALUES('Forbidden','forbidden')",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(statement)
    finally:
        connection.set_authorizer(None)
    assert all(row in _rows(connection) for row in before)


def test_mcp_accepts_only_the_verified_shared_projection(shared_archive):
    from app.core.wechat_mcp_archive import WechatArchiveQueryError, _require_tables

    connection, conversations, _ = shared_archive
    required = {"messages", "conversations", "partitions", "archive_info"}
    _require_tables(connection, "history", required)
    compact_shared_group_bodies(connection, conversations)
    _require_tables(connection, "history", required)
    connection.execute("DROP TRIGGER shared_messages_insert")
    with pytest.raises(WechatArchiveQueryError, match="unsupported message storage format"):
        _require_tables(connection, "history", required)


def test_corrupt_existing_reference_is_not_silently_hidden(shared_archive):
    connection, conversations, identifiers = shared_archive
    compact_shared_group_bodies(connection, conversations)
    # Simulate externally damaged storage after bypassing its write guards.
    triggers = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='message_storage'"
    ).fetchall()
    for (trigger,) in triggers:
        connection.execute('DROP TRIGGER "' + trigger.replace('"', '""') + '"')
    connection.execute(
        "UPDATE message_storage SET content_ref=999999999 WHERE id=?",
        (identifiers[(conversations[0], 2)],),
    )
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        compact_shared_group_bodies(connection, conversations)
