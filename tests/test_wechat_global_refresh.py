"""Global preparation preserves source isolation, sender proof, and idempotency.

Code version: v1.2.2-codex.0
"""

from contextlib import closing
from dataclasses import replace
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_global_refresh as global_refresh
from app.core import wechat_mac_refresh as refresh_service
from app.core import wechat_mac_refresh_projection as projection
from app.core import wechat_mac_refresh_worker as refresh_worker
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_mac_refresh_source import KeyReference, RefreshError
from tests import test_wechat_mac_refresh as refresh_fixtures


mac_archive = refresh_fixtures.mac_archive
refresh_archive = refresh_fixtures.refresh_archive
OWNER = refresh_fixtures.OWNER


class Registry:
    def __init__(self, tmp_path, *, count=1):
        self.calls = []
        self.values = []
        for number in range(count):
            path = tmp_path / f"message_{number}.db"
            path.write_bytes(b"Synthetic encrypted source")
            self.values.append(
                KeyReference(
                    OWNER,
                    OWNER + "_abcd",
                    f"db_storage/message/message_{number}.db",
                    path,
                )
            )

    def references(self, owner, *, load_keys=False):
        self.calls.append((owner, load_keys))
        if owner != OWNER:
            return []
        return [
            replace(value, key=b"k" * 32 if load_keys else None)
            for value in self.values
        ]

    def missing_database_count(self, _references):
        return 2


def _add_scope(history, session, peer="wxid_synthetic_second"):
    with sqlite3.connect(history) as connection:
        connection.row_factory = sqlite3.Row
        current = dict(
            connection.execute("SELECT * FROM conversations WHERE id=?", (session,)).fetchone()
        )
        second = "wechat:" + hashlib.sha256(peer.encode()).hexdigest()
        current.update(id=second, peer_id=peer)
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(current.values()),
        )
        for row in connection.execute(
            "SELECT * FROM messages WHERE conversation_id=?", (session,)
        ).fetchall():
            value = dict(row)
            value.update(
                id=hashlib.sha256((second + row["id"]).encode()).hexdigest(),
                conversation_id=second,
                sender_id=peer,
                sender_label=peer,
            )
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(value.values()),
            )
    return projection.ConversationScope(second, OWNER, peer, "direct")


def _add_other_account(history, session):
    owner = "wxid_separate_account"
    with sqlite3.connect(history) as connection:
        connection.row_factory = sqlite3.Row
        scope = dict(connection.execute(
            "SELECT * FROM conversations WHERE id=?", (session,)
        ).fetchone())
        original_session = scope["id"]
        partition = dict(connection.execute(
            "SELECT * FROM partitions WHERE id=?", (scope["partition_id"],)
        ).fetchone())
        source_id = "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
        account = "account:" + owner
        partition.update(id=source_id, account_id=owner, account_key=account)
        scope.update(
            id="wechat:" + hashlib.sha256(owner.encode()).hexdigest(),
            partition_id=source_id, account_key=account,
        )
        for table, value in (("partitions", partition), ("conversations", scope)):
            marks = ",".join("?" for _ in value)
            connection.execute(f"INSERT INTO {table} VALUES({marks})", tuple(value.values()))
        for row in connection.execute(
            "SELECT * FROM messages WHERE conversation_id=?", (original_session,)
        ).fetchall():
            message = dict(row)
            message.update(
                id=hashlib.sha256((owner + message["id"]).encode()).hexdigest(),
                conversation_id=scope["id"],
            )
            marks = ",".join("?" for _ in message)
            connection.execute(f"INSERT INTO messages VALUES({marks})", tuple(message.values()))
    return projection.ConversationScope(scope["id"], owner, scope["peer_id"], scope["kind"])


def _with_second_database(scope, records, second, *, bad_second=False):
    database = refresh_fixtures._database(scope, records)
    connection = database.connection
    connection.execute(
        "INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(4,?,1)",
        (second.peer_id,),
    )
    table = projection._table(second.peer_id)
    connection.execute(
        'CREATE TABLE "' + table + '"('
        "local_id INTEGER PRIMARY KEY,server_id INTEGER,local_type INTEGER,"
        "create_time INTEGER,real_sender_id INTEGER,message_content BLOB,"
        "WCDB_CT_message_content INTEGER)"
    )
    for row in records:
        value = dict(row)
        if value["real_sender_id"] == 3:
            value["real_sender_id"] = 4
        if bad_second and value["local_id"] <= 2:
            value["message_content"] = "Different source observation"
        connection.execute(
            'INSERT INTO "' + table + '" VALUES(?,?,?,?,?,?,?)', tuple(value.values())
        )
    return database


def test_account_refresh_reads_one_owner_once_and_publishes_all_eligible_chats(
    refresh_archive, tmp_path, monkeypatch,
):
    root, history, session, records = refresh_archive
    original_scope = projection.resolve_scope(root, session)
    other = _add_other_account(history, session)
    second = _add_scope(history, session)
    other_database = refresh_fixtures._database(other, records, owner=other.account_id)
    other_database.account_directory = other.account_id + "_abcd"
    other_database.source.update(
        source_id="macos-refresh-separate-account",
        snapshot_id="synthetic-separate-committed-snapshot",
    )
    with closing(other_database.connection):
        previous, _counts = projection.prepare_packet(root, other, other_database)
    candidate = tmp_path / "other-account-baseline.sqlite3"
    baseline = bank.stage_mac_recovery_bank(root, [previous], candidate, account_ids=[other.account_id])
    bank.publish_mac_recovery_bank(root, candidate, baseline)

    def other_rows(connection):
        return connection.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (other.session_id,)
        ).fetchall()

    with closing(sqlite3.connect(bank.mac_recovery_bank_path(root))) as original_bank:
        other_before = other_rows(original_bank)
    assert len(other_before) == 2
    registry = Registry(tmp_path)
    service = refresh_service.WechatMacRefreshService(root, key_registry=registry, platform="darwin")
    monkeypatch.setattr(refresh_service, "sys", SimpleNamespace(version_info=(3, 14)))
    opened = []

    def read(reference, **_kwargs):
        opened.append(reference.account_id)
        return _with_second_database(original_scope, records, second)

    monkeypatch.setattr(global_refresh, "read_database", read)
    attached_scopes = []
    timeline = refresh_service.WechatArchiveRepository._timeline_tables

    def attach(repository, connection, history_identity, *, conversation_id=None, **options):
        assert conversation_id in {session, second.session_id}
        attached_scopes.append(conversation_id)
        return timeline(
            repository, connection, history_identity, conversation_id=conversation_id, **options,
        )

    monkeypatch.setattr(refresh_service.WechatArchiveRepository, "_timeline_tables", attach)
    assert service.account_available(OWNER)
    assert not service.account_available("account:" + OWNER)
    assert not service.account_available("wxid_not_in_archive")
    assert registry.calls == [(OWNER, False)] and not opened
    before = history.read_bytes()
    try:
        state = refresh_fixtures._wait(service, service.start_account(OWNER)["task_id"])
        assert state["state"] == "succeeded", state
        assert (state["added"], state["total"], state["checked"], state["partial"]) == (4, 0, 8, True)
        assert attached_scopes.count(session) == attached_scopes.count(second.session_id) == 2
        assert opened == [OWNER]
        assert set(registry.calls) == {(OWNER, False), (OWNER, True)}
        assert other.account_id not in {owner for owner, _ in registry.calls}
        assert history.read_bytes() == before
        with closing(sqlite3.connect(bank.mac_recovery_bank_path(root))) as published:
            assert {row[0] for row in published.execute("SELECT account_id FROM sources")} == {
                OWNER, other.account_id,
            }
            assert {row[0] for row in published.execute("SELECT conversation_id FROM messages")} == {
                session, second.session_id, other.session_id,
            }
            assert other_rows(published) == other_before
    finally:
        service.shutdown()


def test_account_scope_delegates_from_python_313_without_loading_keys(
    refresh_archive, tmp_path, monkeypatch,
):
    root, _history, _session, _records = refresh_archive
    registry = Registry(tmp_path)
    service = refresh_service.WechatMacRefreshService(root, key_registry=registry, platform="darwin")
    monkeypatch.setattr(refresh_service, "sys", SimpleNamespace(version_info=(3, 13)))
    requests = []

    def delegate(store, owner, cancelled, *, account, on_progress):
        requests.append((store, owner, account, cancelled.is_set()))
        on_progress({"phase": "reading", "parts_total": 2, "part_current": 1})
        return {
            "state": "succeeded", "phase": "complete", "message": "Chat history refreshed.",
            "added": 0, "total": 2, "partial": False, "checked": 2,
        }

    monkeypatch.setattr(refresh_worker, "run_compatible_worker", delegate)
    try:
        state = refresh_fixtures._wait(service, service.start_account(OWNER)["task_id"])
        assert state["state"] == "succeeded"
        assert requests == [(root, OWNER, True, False)]
        assert registry.calls == [(OWNER, False)]
    finally:
        service.shutdown()


def test_matching_account_string_without_verified_archive_partition_is_unavailable(
    refresh_archive, tmp_path,
):
    root, history, _session, _records = refresh_archive
    registry = Registry(tmp_path)
    service = refresh_service.WechatMacRefreshService(root, key_registry=registry, platform="darwin")
    with sqlite3.connect(history) as connection:
        connection.execute("UPDATE partitions SET identity_status='Corroborated export ID'")
    assert not service.account_available(OWNER)
    with pytest.raises(RefreshError, match="This account cannot be refreshed"):
        service.start_account(OWNER)
    assert registry.calls == []
    service.shutdown()


def test_inventory_reads_metadata_only_and_excludes_ineligible_scopes(
    refresh_archive, tmp_path, monkeypatch
):
    root, history, session, _records = refresh_archive
    official = _add_scope(history, session, "gh_synthetic_official")
    registry = Registry(tmp_path)
    monkeypatch.setattr(
        global_refresh,
        "read_database",
        lambda *_args, **_kwargs: pytest.fail("Inventory must not decrypt"),
    )

    inventory = global_refresh.inventory_global_refresh(root, registry)

    assert inventory.account_ids == (OWNER,)
    assert {scope.session_id for scope in inventory.scopes} == {session}
    assert official.session_id not in {scope.session_id for scope in inventory.scopes}
    assert inventory.summary["configured_sources"] == 1
    assert inventory.summary["configured_source_bytes"] == registry.values[0].database_path.stat().st_size
    assert inventory.summary["unavailable_sources"] == 2
    assert inventory.summary["total_archived_accounts"] == 1
    assert registry.calls == [(OWNER, False)]
    assert all(reference.key is None for reference in inventory.references)


def test_each_shard_is_read_once_and_metadata_is_shared_across_scopes(
    refresh_archive, tmp_path, monkeypatch
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    second = _add_scope(history, session)
    registry = Registry(tmp_path, count=2)
    opened, statements = [], []

    def read(reference, **_kwargs):
        opened.append(reference.relative_path)
        if reference.relative_path.endswith("message_1.db"):
            raise RefreshError("Synthetic private source failure")
        database = _with_second_database(scope, records, second)
        database.connection.set_trace_callback(statements.append)
        return database

    monkeypatch.setattr(global_refresh, "read_database", read)
    progress = []
    packets = list(
        global_refresh.iter_global_refresh_packets(
            root, registry=registry, on_progress=progress.append
        )
    )

    assert len(packets) == 2
    assert {packet["session_id"] for packet in packets} == {session, second.session_id}
    assert len(opened) == len(set(opened)) == 2
    assert sum("FROM Name2Id" in statement for statement in statements) == 1
    for packet in packets:
        bank._validate_packet(packet, (OWNER,))
    final = progress[-1]
    assert final["phase"] == "complete"
    assert (final["checked"], final["candidates"], final["processed_scopes"]) == (8, 4, 2)
    assert final["skipped_sources"] == 1
    assert final["reason_codes"] == {"source_read_or_metadata_failed": 1}
    assert "Synthetic private source failure" not in json.dumps(progress)


def test_preparation_replays_without_new_candidates_after_publication(
    refresh_archive, tmp_path, monkeypatch
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    registry = Registry(tmp_path)
    monkeypatch.setattr(
        global_refresh,
        "read_database",
        lambda *_args, **_kwargs: refresh_fixtures._database(scope, records),
    )
    packets = list(global_refresh.iter_global_refresh_packets(root, registry=registry))
    assert len(packets) == 1 and len(packets[0]["messages"]) == 2
    candidate = tmp_path / "global-candidate.sqlite3"
    receipt = bank.stage_mac_recovery_bank(root, packets, candidate, account_ids=[OWNER])
    bank.publish_mac_recovery_bank(root, candidate, receipt)
    progress = []

    repeated = list(
        global_refresh.iter_global_refresh_packets(
            root, registry=registry, on_progress=progress.append
        )
    )

    assert repeated == []
    assert progress[-1]["checked"] == 4
    assert progress[-1]["candidates"] == 0
    assert receipt["added"] == 2


def test_unbound_scope_is_skipped_without_affecting_an_independent_scope(
    refresh_archive, tmp_path, monkeypatch
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    second = _add_scope(history, session)
    monkeypatch.setattr(
        global_refresh,
        "read_database",
        lambda *_args, **_kwargs: _with_second_database(
            scope, records, second, bad_second=True
        ),
    )
    progress = []
    packets = list(
        global_refresh.iter_global_refresh_packets(
            root, registry=Registry(tmp_path), on_progress=progress.append
        )
    )

    assert [packet["session_id"] for packet in packets] == [session]
    assert progress[-1]["checked"] == 12
    assert progress[-1]["retried_scope_sources"] == 1
    assert progress[-1]["skipped_scopes"] == 1
    assert progress[-1]["reason_codes"] == {"scope_account_binding_unavailable": 1}


def test_exclusion_receipts_preserve_coordinates_without_body_or_exception_text(
    refresh_archive, tmp_path, monkeypatch
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    changed = [dict(row) for row in records]
    changed[2].update(real_sender_id=999, message_content="Private synthetic content")
    monkeypatch.setattr(
        global_refresh,
        "read_database",
        lambda *_args, **_kwargs: refresh_fixtures._database(scope, changed),
    )
    progress, excluded = [], []
    packets = list(
        global_refresh.iter_global_refresh_packets(
            root,
            registry=Registry(tmp_path),
            on_progress=progress.append,
            on_excluded=excluded.append,
        )
    )

    assert [message["server_id"] for message in packets[0]["messages"]] == ["104"]
    assert excluded[0]["reason"] == "row_missing_authenticated_sender"
    assert (excluded[0]["local_id"], excluded[0]["session_id"]) == (3, session)
    assert "Private synthetic content" not in json.dumps(excluded + progress)
    assert progress[-1]["excluded"] == 1
    assert progress[-1]["reason_codes"] == {"row_missing_authenticated_sender": 1}


def test_cancellation_closes_the_current_snapshot_and_is_not_a_skipped_source(
    refresh_archive, tmp_path, monkeypatch
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    database = refresh_fixtures._database(scope, records)
    monkeypatch.setattr(global_refresh, "read_database", lambda *_args, **_kwargs: database)
    cancelled = False

    def check_cancel():
        if cancelled:
            raise RefreshError("Synthetic cancellation")

    iterator = global_refresh.iter_global_refresh_packets(
        root, registry=Registry(tmp_path), check_cancel=check_cancel
    )
    assert next(iterator)["session_id"] == session
    cancelled = True
    with pytest.raises(global_refresh.GlobalRefreshCancelled):
        next(iterator)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.connection.execute("SELECT 1")


def test_oversized_physical_scopes_are_reported_without_partial_projection(
    refresh_archive, tmp_path, monkeypatch
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    monkeypatch.setattr(global_refresh, "MAX_ARCHIVE_ANCHOR_ROWS", 2)
    monkeypatch.setattr(
        global_refresh,
        "read_database",
        lambda *_args, **_kwargs: refresh_fixtures._database(scope, records),
    )
    progress, excluded = [], []
    assert list(
        global_refresh.iter_global_refresh_packets(
            root,
            registry=Registry(tmp_path),
            on_progress=progress.append,
            on_excluded=excluded.append,
        )
    ) == []
    assert excluded[0]["native_rows"] == 4
    assert excluded[0]["reason"] == "scope_native_row_budget_exceeded"
    assert progress[-1]["oversized_scopes"] == 1
    assert progress[-1]["checked"] == progress[-1]["candidates"] == 0


def test_source_context_rejects_mutation_after_metadata_preparation(refresh_archive):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    database = refresh_fixtures._database(scope, records)
    try:
        context = projection.prepare_source_context(OWNER, database)
        database.connection.execute("UPDATE Name2Id SET user_name='Changed sender' WHERE rowid=3")
        with pytest.raises(RefreshError, match="context changed"):
            projection.prepare_packet(root, scope, database, context=context)
    finally:
        database.close()


@pytest.mark.parametrize(
    "selection",
    [
        frozenset({("wxid_unrelated_owner", "db_storage/message/message_0.db", "session")}),
        frozenset({(OWNER, "db_storage/message/unknown.db", "session")}),
        frozenset({(OWNER, "db_storage/message/message_0.db", "wechat:" + "f" * 64)}),
        frozenset({(OWNER, "db_storage/message/message_0.db")}),
    ],
)
def test_invalid_retry_selections_fail_before_any_database_read(
    refresh_archive, tmp_path, monkeypatch, selection
):
    root, _history, _session, _records = refresh_archive
    registry = Registry(tmp_path)
    inventory = global_refresh.inventory_global_refresh(root, registry)
    monkeypatch.setattr(
        global_refresh,
        "read_database",
        lambda *_args, **_kwargs: pytest.fail("Invalid selection read a source"),
    )
    with pytest.raises(RefreshError, match="selected global scopes"):
        list(
            global_refresh.iter_global_refresh_packets(
                root, registry=registry, inventory=inventory, scope_sources=selection
            )
        )
    assert registry.calls == [(OWNER, False)]


def test_retry_selection_never_reads_an_unselected_source(
    refresh_archive, tmp_path, monkeypatch
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    registry = Registry(tmp_path, count=2)
    opened = []

    def read(reference, **_kwargs):
        opened.append(reference.relative_path)
        assert reference.relative_path.endswith("message_1.db")
        return refresh_fixtures._database(scope, records)

    monkeypatch.setattr(global_refresh, "read_database", read)
    inventory = global_refresh.inventory_global_refresh(root, registry)
    progress = []
    assert len(list(
        global_refresh.iter_global_refresh_packets(
            root,
            registry=registry,
            inventory=inventory,
            scope_sources=frozenset({(OWNER, "db_storage/message/message_1.db", session)}),
            on_progress=progress.append,
        )
    )) == 1
    assert opened == ["db_storage/message/message_1.db"]
    assert progress[-1]["selected_sources"] == progress[-1]["selected_scope_sources"] == 1


def test_empty_retry_selection_reads_no_key_values_or_sources(
    refresh_archive, tmp_path, monkeypatch
):
    root, _history, _session, _records = refresh_archive
    registry = Registry(tmp_path)
    inventory = global_refresh.inventory_global_refresh(root, registry)
    monkeypatch.setattr(
        global_refresh,
        "read_database",
        lambda *_args, **_kwargs: pytest.fail("Empty selection read a source"),
    )
    assert list(
        global_refresh.iter_global_refresh_packets(
            root, registry=registry, inventory=inventory, scope_sources=frozenset()
        )
    ) == []
    assert registry.calls == [(OWNER, False)]


def test_chunked_projection_retains_original_anchors_and_default_single_packet_limit(
    refresh_archive, monkeypatch
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    database = refresh_fixtures._database(scope, records)
    monkeypatch.setattr(bank, "MAX_PACKET_MESSAGES", 3)
    try:
        with pytest.raises(RefreshError, match="bounded native refresh budget"):
            projection.prepare_packet(root, scope, database)
        counts = []
        packets = list(
            projection.iter_prepare_packets(root, scope, database, on_counts=counts.append)
        )
        assert [value["total"] for value in counts] == [3, 1]
        assert [[message["server_id"] for message in packet["messages"]] for packet in packets] == [["103"], ["104"]]
        assert packets[0]["anchors"] == packets[1]["anchors"]
        for packet in packets:
            bank._validate_packet(packet, (OWNER,))
    finally:
        database.close()


def test_chunked_projection_splits_by_serialized_bytes_without_losing_rows(
    refresh_archive, monkeypatch
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    changed = [dict(row) for row in records]
    changed[2]["message_content"] = "Large synthetic event " + "a" * 3000
    changed[3]["message_content"] = "Large owner event " + "b" * 3000
    database = refresh_fixtures._database(scope, changed)
    monkeypatch.setattr(bank, "MAX_PACKET_BYTES", 12_000)
    try:
        counts = []
        packets = list(
            projection.iter_prepare_packets(root, scope, database, on_counts=counts.append)
        )
        assert len(packets) == 2
        assert sum(value["total"] for value in counts) == 4
        assert {message["local_id"] for packet in packets for message in packet["messages"]} == {3, 4}
        assert all(len(bank._json(packet).encode()) <= 12_000 for packet in packets)
    finally:
        database.close()


def test_exact_100520_native_rows_include_the_final_520_and_replay_idempotently(
    refresh_archive, tmp_path
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)

    def database():
        value = refresh_fixtures._database(scope, records[:2])
        table = projection._table(scope.peer_id)
        seed = records[0]
        rows = (
            (
                local,
                seed["server_id"] if local <= 100_000 else 100_000_000 + local,
                1,
                seed["create_time"] if local <= 100_000 else 1_700_000_000 + local,
                3,
                seed["message_content"] if local <= 100_000 else "Bounded tail event",
                0,
            )
            for local in range(3, 100_521)
        )
        value.connection.executemany(
            'INSERT INTO "' + table + '" VALUES(?,?,?,?,?,?,?)', rows
        )
        return value

    native = database()
    try:
        counts = []
        packets = list(
            projection.iter_prepare_packets(root, scope, native, on_counts=counts.append)
        )
    finally:
        native.close()
    assert [value["total"] for value in counts] == [100_000, 520]
    assert len(packets) == 1 and len(packets[0]["messages"]) == 520
    assert {message["local_id"] for message in packets[0]["messages"]} == set(range(100_001, 100_521))
    candidate = tmp_path / "chunk-candidate.sqlite3"
    receipt = bank.stage_mac_recovery_bank(root, packets, candidate, account_ids=[OWNER])
    bank.publish_mac_recovery_bank(root, candidate, receipt)
    assert receipt["added"] == 520
    repeated = database()
    try:
        assert list(projection.iter_prepare_packets(root, scope, repeated)) == []
    finally:
        repeated.close()


def _learning_source(refresh_archive):
    root, history, session, original_records = refresh_archive
    scope = projection.resolve_scope(root, session)
    second = _add_scope(history, session)
    target = _add_scope(history, session, "wxid_synthetic_unanchored")
    records = [dict(row) for row in original_records]
    records[0]["real_sender_id"] = 2
    with sqlite3.connect(history) as connection:
        connection.execute(
            "UPDATE messages SET sender_id=?,sender_label=?,direction='sent' "
            "WHERE conversation_id=? AND ordinal=1",
            (OWNER, OWNER, session),
        )
    database = _with_second_database(scope, records, second)
    database.connection.execute(
        "INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(5,?,1)",
        (target.peer_id,),
    )
    table = projection._table(target.peer_id)
    database.connection.execute(
        'CREATE TABLE "' + table + '"('
        "local_id INTEGER PRIMARY KEY,server_id INTEGER,local_type INTEGER,"
        "create_time INTEGER,real_sender_id INTEGER,message_content BLOB,"
        "WCDB_CT_message_content INTEGER)"
    )
    for row in records[2:]:
        value = dict(row)
        if value["real_sender_id"] == 3:
            value["real_sender_id"] = 5
        database.connection.execute(
            'INSERT INTO "' + table + '" VALUES(?,?,?,?,?,?,?)', tuple(value.values())
        )
    return root, history, scope, second, target, database


def test_later_authenticated_witnesses_recover_a_deferred_scope_without_duplicate_packets(
    refresh_archive, tmp_path, monkeypatch
):
    root, _history, scope, second, target, database = _learning_source(refresh_archive)
    registry = Registry(tmp_path)
    inventory = global_refresh.inventory_global_refresh(root, registry)
    inventory = replace(inventory, scopes=(target, scope, second))
    calls = []
    original = projection._global_binding

    def binding(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(projection, "_global_binding", binding)
    monkeypatch.setattr(global_refresh, "read_database", lambda *_args, **_kwargs: database)
    progress, excluded = [], []
    packets = list(
        global_refresh.iter_global_refresh_packets(
            root,
            registry=registry,
            inventory=inventory,
            on_progress=progress.append,
            on_excluded=excluded.append,
        )
    )

    assert [packet["session_id"] for packet in packets].count(target.session_id) == 1
    recovered = next(packet for packet in packets if packet["session_id"] == target.session_id)
    assert len(recovered["account_binding"]["anchors"]) == 2
    assert len({anchor["peer_id"] for anchor in recovered["account_binding"]["anchors"]}) == 2
    assert any(anchor["direction"] == "sent" for anchor in recovered["account_binding"]["anchors"])
    assert len(calls) == 2
    assert progress[-1]["skipped_scopes"] == 0
    assert progress[-1]["retried_scope_sources"] == progress[-1]["recovered_scope_sources"] == 1
    assert not excluded
    for packet in packets:
        bank._validate_packet(packet, (OWNER,))


def test_learned_witnesses_are_revalidated_against_exact_primary_account(
    refresh_archive
):
    root, history, scope, second, target, database = _learning_source(refresh_archive)
    try:
        context = projection.prepare_source_context(OWNER, database, _learn_witnesses=True)
        projection.prepare_packet(root, scope, database, context=context)
        projection.prepare_packet(root, second, database, context=context)
        sent = next(
            value for value in context.learned_witnesses.values()
            if value["direction"] == "sent"
        )
        received = next(
            value for value in context.learned_witnesses.values()
            if value["peer_id"] == second.peer_id
        )
        foreign_id = hashlib.sha256(b"foreign-account-message").hexdigest()
        foreign_session = "wechat:" + hashlib.sha256(b"foreign-account-scope").hexdigest()
        foreign_owner = "wxid_synthetic_foreign_owner"
        foreign_partition = "ios-" + hashlib.md5(foreign_owner.encode(), usedforsecurity=False).hexdigest()
        with sqlite3.connect(history) as connection:
            connection.row_factory = sqlite3.Row
            partition = dict(connection.execute("SELECT * FROM partitions LIMIT 1").fetchone())
            partition.update(id=foreign_partition, account_id=foreign_owner, account_key="account:" + foreign_owner)
            connection.execute("INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)", tuple(partition.values()))
            conversation = dict(connection.execute("SELECT * FROM conversations WHERE id=?", (second.session_id,)).fetchone())
            conversation.update(id=foreign_session, partition_id=foreign_partition, account_key="account:" + foreign_owner)
            connection.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", tuple(conversation.values()))
            row = dict(connection.execute("SELECT * FROM messages WHERE id=?", (received["archive_message_id"],)).fetchone())
            row.update(id=foreign_id, conversation_id=foreign_session)
            connection.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row.values()))
        crossed = {**received, "archive_message_id": foreign_id}
        source = {**database.source, "table": projection._table(target.peer_id), "account_directory": database.account_directory}
        with sqlite3.connect(history.as_uri() + "?mode=ro&immutable=1", uri=True) as primary:
            primary.row_factory = sqlite3.Row
            with pytest.raises(projection.SourceAccountBindingUnavailable):
                projection._global_binding(
                    root, database.connection, primary, target, source,
                    database.account_directory, context.names, context.peer_rows,
                    context.tables, lambda: None, learned_witnesses=(sent, crossed),
                )
    finally:
        database.close()


def test_full_scope_guard_remains_the_existing_anchor_row_budget(
    refresh_archive, monkeypatch
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    database = refresh_fixtures._database(scope, records)
    statements = []
    database.connection.set_trace_callback(statements.append)
    monkeypatch.setattr(projection, "MAX_ARCHIVE_ANCHOR_ROWS", 3)
    try:
        with pytest.raises(RefreshError, match="bounded global row budget"):
            list(projection.iter_prepare_packets(root, scope, database))
        assert not any("SELECT local_id" in statement for statement in statements)
    finally:
        database.close()
