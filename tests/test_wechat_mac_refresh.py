"""Explicit refresh preserves canonical scope, idempotency, totals and cancellation.

Code version: v1.0.0-codex.0
"""

from datetime import datetime
import hashlib
import json
import sqlite3
from threading import Event
from types import SimpleNamespace
import time

import pytest

from app.core import wechat_mac_refresh as refresh
from app.core import wechat_mac_refresh_projection as projection
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_mac_refresh_source import AuthenticatedDatabase, RefreshError
from tests import test_wechat_macos_recovery as fixtures


@pytest.fixture(autouse=True)
def inprocess_runtime(monkeypatch):
    # These fixtures exercise the core pipeline on every host; controlled older
    # runtime delegation is covered separately with transport fakes.
    monkeypatch.setattr(
        refresh, "sys", SimpleNamespace(version_info=(3, 14), platform="darwin")
    )


mac_archive = fixtures.mac_archive
OWNER = fixtures.OWNER
PEER = fixtures.PEER
MEMBER = "wxid_synthetic_member"
ROOM = "123456789@chatroom"


@pytest.fixture
def refresh_archive(mac_archive):
    root, history, session, _packet = mac_archive
    records = []
    with sqlite3.connect(history) as connection:
        connection.row_factory = sqlite3.Row
        for index, row in enumerate(
            connection.execute("SELECT * FROM messages ORDER BY ordinal").fetchall(), 1
        ):
            epoch = int(datetime.fromisoformat(row["timestamp_value"]).timestamp())
            proof = json.loads(row["provenance"])
            proof["unix_timestamp"] = epoch
            connection.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(proof), row["id"]),
            )
            records.append(
                {
                    "local_id": index,
                    "server_id": int(proof["server_id"]),
                    "local_type": 1,
                    "create_time": epoch,
                    "real_sender_id": 3,
                    "message_content": row["text"],
                    "WCDB_CT_message_content": 0,
                }
            )
    records.extend(
        [
            {
                "local_id": 3,
                "server_id": 103,
                "local_type": 1,
                "create_time": 1_700_000_100,
                "real_sender_id": 3,
                "message_content": "New literal message",
                "WCDB_CT_message_content": 0,
            },
            {
                "local_id": 4,
                "server_id": 104,
                "local_type": 1,
                "create_time": 1_700_000_101,
                "real_sender_id": 2,
                "message_content": "Owner reply",
                "WCDB_CT_message_content": 0,
            },
        ]
    )
    return root, history, session, records


def _database(scope, records, *, owner=OWNER, include_target=True):
    native = sqlite3.connect(":memory:")
    native.row_factory = sqlite3.Row
    native.execute("CREATE TABLE Name2Id(user_name TEXT,is_session INTEGER)")
    sender = MEMBER if scope.kind == "group" else scope.peer_id
    native.executemany(
        "INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(?,?,?)",
        [(2, owner, 0), (3, sender, 1)],
    )
    if scope.kind == "group":
        native.execute(
            "INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(4,?,1)",
            (scope.peer_id,),
        )
    table = projection._table(scope.peer_id)
    if include_target:
        native.execute(
            'CREATE TABLE "'
            + table
            + '"(local_id INTEGER PRIMARY KEY,server_id INTEGER,'
            "local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,"
            "message_content BLOB,WCDB_CT_message_content INTEGER)"
        )
        for row in records:
            values = dict(row)
            if (
                scope.kind == "group"
                and values["real_sender_id"] == 3
                and isinstance(values["message_content"], str)
            ):
                values["message_content"] = MEMBER + ":\n" + values["message_content"]
            native.execute(
                'INSERT INTO "' + table + '" VALUES(?,?,?,?,?,?,?)',
                tuple(values.values()),
            )
    source = {
        "source_id": "macos-refresh-synthetic",
        "snapshot_id": "synthetic-committed-snapshot",
        "main_sha256": "a" * 64,
        "wal_sha256": "b" * 64,
        "database_relative_path": "db_storage/message/message_0.db",
        "integrity_check": "ok",
        "authenticated_pages": True,
        "page_count": 1,
        "authenticated_page_count": 1,
    }
    return AuthenticatedDatabase(native, source, OWNER + "_abcd", False)


class Registry:
    def __init__(self, missing=0):
        self.missing = missing
        self.loaded_keys = 0

    def references(self, owner, *, load_keys=False):
        self.loaded_keys += bool(load_keys)
        return [object()] if owner == OWNER else []

    def missing_database_count(self, references):
        return self.missing


def _wait(service, token):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = service.status(token)
        if state["state"] != "running":
            return state
        time.sleep(0.005)
    pytest.fail("The bounded synthetic refresh did not finish.")


def test_projection_contains_only_new_target_events_and_replays_without_new_candidates(
    refresh_archive,
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    original = history.read_bytes()
    native = _database(scope, records)
    try:
        packet, counts = projection.prepare_packet(root, scope, native)
        assert counts == {"total": 4, "existing": 2, "excluded": 0}
        assert [message["server_id"] for message in packet["messages"]] == [
            "103",
            "104",
        ]
        assert packet["session_id"] == session and packet["account_id"] == OWNER
        assert len(packet["anchors"]) == 2
        candidate = history.parent / "synthetic-candidate.sqlite3"
        receipt = bank.stage_mac_recovery_bank(
            root, [packet], candidate, account_ids=[OWNER]
        )
        bank.publish_mac_recovery_bank(root, candidate, receipt)
        bank_identity = bank._identity(bank.mac_recovery_bank_path(root))
        repeated, counts = projection.prepare_packet(root, scope, native)
        assert repeated is None and counts == {"total": 4, "existing": 4, "excluded": 0}
        assert bank._identity(bank.mac_recovery_bank_path(root)) == bank_identity
        assert history.read_bytes() == original
    finally:
        native.close()


def test_group_refresh_retains_member_identity_and_literal_body(refresh_archive):
    root, history, session, records = refresh_archive
    with sqlite3.connect(history) as connection:
        connection.execute(
            "UPDATE conversations SET peer_id=?,kind='group' WHERE id=?",
            (ROOM, session),
        )
        connection.execute(
            "UPDATE messages SET sender_id=?,sender_label=? WHERE conversation_id=?",
            (MEMBER, MEMBER, session),
        )
    scope = projection.resolve_scope(root, session)
    native = _database(scope, records)
    try:
        packet, counts = projection.prepare_packet(root, scope, native)
        assert packet["conversation_kind"] == "group"
        assert counts["existing"] == 2
        assert packet["messages"][0]["sender_id"] == MEMBER
        assert packet["messages"][0]["text"] == "New literal message"
        assert packet["messages"][1]["direction"] == "sent"
        bank._validate_packet(packet, (OWNER,))
    finally:
        native.close()


def test_missing_dictionary_is_explicitly_excluded_without_inventing_text(
    refresh_archive, monkeypatch
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    native = _database(scope, records)
    decode = projection.decode_wcdb_text

    def unavailable(value, marker):
        if value == "New literal message":
            raise projection.WCDBDecodeError("Synthetic unavailable dictionary")
        return decode(value, marker)

    monkeypatch.setattr(projection, "decode_wcdb_text", unavailable)
    try:
        packet, counts = projection.prepare_packet(root, scope, native)
        assert counts["excluded"] == 1
        assert [message["server_id"] for message in packet["messages"]] == ["104"]
    finally:
        native.close()


def test_wrong_owner_is_rejected_even_when_requested_table_is_absent(refresh_archive):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    native = _database(scope, records, owner="wxid_wrong_owner", include_target=False)
    try:
        with pytest.raises(RefreshError, match="owner"):
            projection.prepare_packet(root, scope, native)
    finally:
        native.close()


def test_unprovable_conversation_does_not_search_other_chat_bodies(
    refresh_archive, monkeypatch
):
    root, history, session, records = refresh_archive
    with sqlite3.connect(history) as connection:
        connection.execute("DELETE FROM messages WHERE ordinal=2")
    scope = projection.resolve_scope(root, session)
    native = _database(scope, records)
    statements = []
    native.connection.set_trace_callback(statements.append)
    try:
        monkeypatch.setattr(projection, "_stored_witnesses", lambda *_args: [])
        with pytest.raises(RefreshError, match="independently revalidated"):
            projection.prepare_packet(root, scope, native)
        assert not any("FROM conversations" in value for value in statements)
        assert not any("ORDER BY c.id" in value for value in statements)
    finally:
        native.close()


def test_service_is_explicit_scoped_and_reports_full_canonical_total(
    refresh_archive, monkeypatch
):
    root, history, session, records = refresh_archive
    registry = Registry(missing=1)
    service = refresh.WechatMacRefreshService(
        root, key_registry=registry, platform="darwin"
    )
    scope = projection.resolve_scope(root, session)
    reads = []

    def reader(_reference, **_kwargs):
        reads.append(True)
        return _database(scope, records)

    monkeypatch.setattr(refresh, "read_database", reader)
    assert service.available(OWNER, PEER, "direct")
    assert registry.loaded_keys == 0 and reads == []
    assert not service.available("wxid_another_owner", PEER, "direct")
    assert not service.available(OWNER, "gh_synthetic", "direct")
    original = history.read_bytes()
    state = _wait(service, service.start(session)["task_id"])
    assert state["state"] == "succeeded", state
    assert (state["added"], state["total"], state["checked"], state["partial"]) == (
        2,
        4,
        4,
        True,
    )
    assert history.read_bytes() == original
    assert not list(history.parent.glob(".mac-refresh-*"))
    assert service.status("not-a-token") is None
    service.shutdown()


def test_thread_start_failure_releases_the_service_busy_state(
    refresh_archive, monkeypatch
):
    root, _history, session, _records = refresh_archive
    service = refresh.WechatMacRefreshService(
        root, key_registry=Registry(), platform="darwin"
    )

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("Synthetic thread allocation failure")

    monkeypatch.setattr(refresh, "Thread", BrokenThread)
    for _ in range(2):
        with pytest.raises(RefreshError, match="worker could not be started"):
            service.start(session)
    assert service._active is None and not service._tasks


def test_duplicate_start_reuses_task_and_shutdown_cancels_before_publication(
    refresh_archive, monkeypatch
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    service = refresh.WechatMacRefreshService(
        root, key_registry=Registry(), platform="darwin"
    )
    entered, release = Event(), Event()

    def reader(_reference, **_kwargs):
        entered.set()
        assert release.wait(5)
        return _database(scope, records)

    monkeypatch.setattr(refresh, "read_database", reader)
    first = service.start(session)
    assert entered.wait(5)
    assert service.start(session)["task_id"] == first["task_id"]
    service.shutdown(wait=True, timeout=0.01)
    release.set()
    state = _wait(service, first["task_id"])
    assert state["state"] == "failed"
    assert not bank.mac_recovery_bank_path(root).exists()
    assert not list(history.parent.glob(".mac-refresh-*"))


def test_source_errors_do_not_expose_keys_paths_or_messages(
    refresh_archive, monkeypatch
):
    root, _history, session, _records = refresh_archive
    secret = "synthetic-key-and-private-source-path"
    service = refresh.WechatMacRefreshService(
        root, key_registry=Registry(), platform="darwin"
    )

    def fail(*_args, **_kwargs):
        raise ValueError(secret)

    monkeypatch.setattr(refresh, "read_database", fail)
    state = _wait(service, service.start(session)["task_id"])
    assert state["state"] == "failed" and state["partial"]
    assert secret not in json.dumps(state)


def test_archive_drift_rejects_publication_without_overwriting_new_state(
    refresh_archive, monkeypatch
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    service = refresh.WechatMacRefreshService(
        root, key_registry=Registry(), platform="darwin"
    )
    monkeypatch.setattr(
        refresh, "read_database", lambda *_args, **_kwargs: _database(scope, records)
    )
    prepare = projection.prepare_packet

    def drift(*args, **kwargs):
        result = prepare(*args, **kwargs)
        history.touch()
        return result

    monkeypatch.setattr(refresh, "prepare_packet", drift)
    state = _wait(service, service.start(session)["task_id"])
    assert state["state"] == "failed" and not bank.mac_recovery_bank_path(root).exists()


def test_cancellation_is_checked_inside_canonical_publish_and_owned_candidate_is_cleaned(
    refresh_archive, monkeypatch
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    service = refresh.WechatMacRefreshService(
        root, key_registry=Registry(), platform="darwin"
    )
    monkeypatch.setattr(
        refresh, "read_database", lambda *_args, **_kwargs: _database(scope, records)
    )
    publish = bank.publish_mac_recovery_bank
    entered = []

    def cancel_at_publication(*args, before_replace=None, **kwargs):
        assert before_replace is not None
        entered.append(True)
        service.shutdown(wait=False)
        return publish(*args, before_replace=before_replace, **kwargs)

    monkeypatch.setattr(bank, "publish_mac_recovery_bank", cancel_at_publication)
    state = _wait(service, service.start(session)["task_id"])
    assert entered and state["state"] == "failed"
    assert not bank.mac_recovery_bank_path(root).exists()
    assert not list(history.parent.glob(".mac-refresh-*"))


def test_older_runtime_delegates_without_loading_keys_in_the_parent(
    refresh_archive, monkeypatch
):
    from app.core import wechat_mac_refresh_worker

    root, _history, session, _records = refresh_archive
    registry = Registry()
    service = refresh.WechatMacRefreshService(
        root, key_registry=registry, platform="darwin"
    )
    monkeypatch.setattr(refresh, "sys", SimpleNamespace(version_info=(3, 13)))
    received = []

    def delegate(store, requested, cancelled):
        received.append((store, requested, cancelled))
        return {
            "state": "succeeded",
            "message": "Chat history refreshed.",
            "added": 0,
            "total": 2,
            "partial": False,
            "checked": 2,
        }

    monkeypatch.setattr(wechat_mac_refresh_worker, "run_compatible_worker", delegate)
    state = _wait(service, service.start(session)["task_id"])
    assert state["state"] == "succeeded" and state["total"] == 2
    assert received[0][:2] == (root, session) and registry.loaded_keys == 0


def test_global_binding_revalidates_only_known_coordinate_witnesses(refresh_archive):
    root, history_path, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    native = _database(scope, records)
    other = "wxid_synthetic_second_witness"
    other_session = "wechat:" + hashlib.sha256(other.encode()).hexdigest()
    other_id = hashlib.sha256(b"synthetic-other-owner-witness").hexdigest()
    native.connection.execute(
        "INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(5,?,1)", (other,)
    )
    table = projection._table(other)
    native.connection.execute(
        'CREATE TABLE "' + table + '"(local_id INTEGER PRIMARY KEY,server_id INTEGER,'
        "local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,"
        "message_content BLOB,WCDB_CT_message_content INTEGER)"
    )
    native.connection.execute(
        'INSERT INTO "' + table + '" VALUES(51,501,1,1700000500,2,?,0)',
        ("Owner witness",),
    )
    with sqlite3.connect(history_path) as writable:
        writable.row_factory = sqlite3.Row
        conversation = dict(
            writable.execute(
                "SELECT * FROM conversations WHERE id=?", (session,)
            ).fetchone()
        )
        conversation.update(id=other_session, peer_id=other, message_count=1)
        writable.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(conversation.values()),
        )
        archived = dict(
            writable.execute(
                "SELECT * FROM messages ORDER BY ordinal LIMIT 1"
            ).fetchone()
        )
        archived.update(
            id=other_id,
            conversation_id=other_session,
            ordinal=1,
            sender_id=OWNER,
            sender_label=OWNER,
            direction="sent",
            text="Owner witness",
            time_sort=1_700_000_500,
            timestamp_value=datetime.fromtimestamp(
                1_700_000_500, tz=datetime.now().astimezone().tzinfo
            ).isoformat(),
            provenance=json.dumps(
                {"server_id": "501", "message_type": 1, "unix_timestamp": 1_700_000_500}
            ),
            search_text="owner witness",
        )
        writable.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(archived.values()),
        )
    history = sqlite3.connect(
        history_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    history.row_factory = sqlite3.Row
    names = {
        row["rowid"]: dict(row)
        for row in native.connection.execute(
            "SELECT rowid,user_name,is_session FROM Name2Id"
        )
    }
    peers = {row["user_name"]: row for row in names.values()}
    source = dict(
        native.source,
        table=projection._table(PEER),
        account_directory=native.account_directory,
    )
    anchors = []
    for peer, local, identifier in ((PEER, 1, None), (other, 51, other_id)):
        original = (
            history.execute(
                "SELECT * FROM messages WHERE id=?", (identifier,)
            ).fetchone()
            if identifier
            else history.execute(
                "SELECT * FROM messages ORDER BY ordinal LIMIT 1"
            ).fetchone()
        )
        row = projection._native_rows(
            native.connection, projection._table(peer), local_id=local
        ).fetchone()
        message = projection._project_message(row, OWNER, "direct", names)
        anchors.append(
            projection._archive_anchor(original, row, message, peer, peers[peer])
        )
    assert all(anchors)
    binding = {
        "schema_version": 1,
        "account_id": OWNER,
        "account_directory": native.account_directory,
        "owner_proof": projection._name_proof(names[2]),
        "source": {field: source[field] for field in projection._SOURCE_BINDING_FIELDS},
        "anchors": anchors,
    }
    packet = {"account_id": OWNER, "source": source, "account_binding": binding}
    with sqlite3.connect(bank.mac_recovery_bank_path(root)) as stored:
        stored.executescript(
            "CREATE TABLE sources(id TEXT,account_id TEXT,proof TEXT);"
            "CREATE TABLE conversation_proofs(conversation_id TEXT,source_id TEXT,proof TEXT,PRIMARY KEY(conversation_id,source_id));"
        )
        stored.execute(
            "INSERT INTO sources VALUES('proof',?,?)", (OWNER, json.dumps(packet))
        )
        stored.execute(
            "INSERT INTO conversation_proofs VALUES(?,'proof',?)",
            (session, json.dumps(packet)),
        )
    statements = []
    native.connection.set_trace_callback(statements.append)
    try:
        result = projection._global_binding(
            root,
            native.connection,
            history,
            scope,
            source,
            native.account_directory,
            names,
            peers,
            {projection._table(PEER), table},
            lambda: None,
        )
        assert {anchor["peer_id"] for anchor in result["anchors"]} == {PEER, other}
        assert any(anchor["direction"] == "sent" for anchor in result["anchors"])
        body_queries = [
            statement for statement in statements if "SELECT local_id" in statement
        ]
        assert len(body_queries) == 2 and all(
            "WHERE local_id=" in query for query in body_queries
        )
        native.connection.execute(
            'UPDATE "' + table + '" SET message_content=? WHERE local_id=51',
            ("Changed witness",),
        )
        with pytest.raises(RefreshError, match="independently revalidated"):
            projection._global_binding(
                root,
                native.connection,
                history,
                scope,
                source,
                native.account_directory,
                names,
                peers,
                {projection._table(PEER), table},
                lambda: None,
            )
    finally:
        native.close()
        history.close()
