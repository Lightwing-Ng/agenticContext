"""Explicit refresh preserves canonical scope, idempotency, totals and cancellation.

Code version: v1.4.4-codex.0
"""

from datetime import datetime
import hashlib
import json
import sqlite3
from threading import Event
from types import SimpleNamespace
import time

from flask import Blueprint, Flask
import pytest

from app.core import wechat_group_roster_source as roster_source
from app.core import wechat_mac_refresh as refresh
from app.core import wechat_mac_refresh_projection as projection
from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_mac_refresh_cipher as cipher
from app.core.wechat_archive import WechatTimelineProjectionError
from app.core.wechat_group_rosters import WechatGroupRosterRepository
from app.core.wechat_mac_refresh_source import (
    AuthenticatedDatabase,
    RefreshError,
    RefreshIssue,
    RefreshNotice,
    SourceAccountBindingUnavailable,
)
from app.web.wechat_refresh_routes import WECHAT_REFRESH_EXTENSION, register_wechat_refresh_routes
from tests import test_wechat_macos_recovery as fixtures
from tests import test_wechat_macos_recovery_bank_upgrades as upgrade_fixtures
from tests.test_wechat_group_rosters import LATE, snapshot
from tests.test_wechat_text import _system_link_template, _system_profile_link


@pytest.fixture(autouse=True)
def inprocess_runtime(monkeypatch):
    # These fixtures exercise the core pipeline on every host; controlled older
    # runtime delegation is covered separately with transport fakes.
    monkeypatch.setattr(
        refresh, "sys", SimpleNamespace(version_info=(3, 14), platform="darwin")
    )


mac_archive = fixtures.mac_archive
bank_archive = upgrade_fixtures.bank_archive
app_stubs = upgrade_fixtures.app_stubs
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


@pytest.fixture
def group_refresh_archive(refresh_archive):
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
    def __init__(self, missing=0, reference_count=1):
        self.missing = missing
        self.reference_count = reference_count
        self.loaded_keys = 0

    def references(self, owner, *, load_keys=False):
        self.loaded_keys += bool(load_keys)
        return [object() for _ in range(self.reference_count)] if owner == OWNER else []

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


def _large_bank_publication(refresh_archive, monkeypatch, *, bank_gib=3, capacity_delta=0):
    """Model a paged sealed bank without allocating or copying a multi-gigabyte file."""
    root, history, session, _records = refresh_archive
    service = refresh.WechatMacRefreshService(
        root, key_registry=Registry(), platform="darwin"
    )
    bank_bytes = bank_gib * 1024**3
    packets = [{"session_id": session}]
    packet_bytes = sum(len(bank._json(packet).encode()) for packet in packets)
    required = bank_bytes + packet_bytes * 3 + refresh.FREE_SPACE_RESERVE
    baseline = {"history.sqlite3": {"size": history.stat().st_size}}
    sealed_bank = SimpleNamespace(
        exists=lambda: True, stat=lambda: SimpleNamespace(st_size=bank_bytes)
    )
    monkeypatch.setattr(bank, "mac_recovery_bank_path", lambda _root: sealed_bank)
    monkeypatch.setattr(bank, "_inputs", lambda _history: baseline)
    monkeypatch.setattr(
        refresh.shutil, "disk_usage",
        lambda _directory: SimpleNamespace(free=required + capacity_delta),
    )
    staged, published = [], []
    receipt = {"input_identities": baseline, "added": 0}

    def stage(_root, observed_packets, candidate, **kwargs):
        staged.append(candidate)
        assert observed_packets == packets
        assert kwargs["account_ids"] == [OWNER]
        assert kwargs["conversation_id"] == session
        assert kwargs["clone_baseline"] is True
        assert candidate.parent.is_dir()
        return receipt

    def publish(_root, candidate, observed_receipt, *, before_replace, reuse_validation, check_cancel):
        assert reuse_validation is True
        assert candidate == staged[0]
        assert observed_receipt == receipt
        check_cancel()
        before_replace()
        published.append(True)
        return {"published": True}

    monkeypatch.setattr(bank, "stage_mac_recovery_bank", stage)
    monkeypatch.setattr(bank, "publish_mac_recovery_bank", publish)
    return SimpleNamespace(
        service=service, root=root, history=history, packets=packets,
        baseline=baseline, staged=staged, published=published, receipt=receipt,
    )


@pytest.mark.parametrize("bank_gib", [3, 12])
def test_large_paged_bank_is_admitted_when_candidate_disk_capacity_is_available(
    refresh_archive, monkeypatch, bank_gib
):
    operation = _large_bank_publication(refresh_archive, monkeypatch, bank_gib=bank_gib)

    result = operation.service._publish(operation.packets, OWNER, operation.baseline)

    assert result == {"published": True}
    assert len(operation.staged) == 1 and operation.published == [True]
    assert not list(operation.history.parent.glob(".mac-refresh-*"))


@pytest.mark.parametrize("bank_gib", [3, 12])
def test_large_bank_with_insufficient_candidate_capacity_is_rejected_before_staging(
    refresh_archive, monkeypatch, bank_gib
):
    operation = _large_bank_publication(
        refresh_archive, monkeypatch, bank_gib=bank_gib, capacity_delta=-1,
    )

    with pytest.raises(RefreshNotice) as error:
        operation.service._publish(operation.packets, OWNER, operation.baseline)
    assert error.value.issue == RefreshIssue.DISK_SPACE

    assert operation.staged == operation.published == []
    assert not list(operation.history.parent.glob(".mac-refresh-*"))


def test_large_bank_admission_retains_the_packet_memory_budget(refresh_archive, monkeypatch):
    operation = _large_bank_publication(refresh_archive, monkeypatch)
    monkeypatch.setattr(refresh, "MAX_PACKET_BYTES", 1)

    with pytest.raises(RefreshNotice) as error:
        operation.service._publish(operation.packets, OWNER, operation.baseline)
    assert error.value.issue == RefreshIssue.MESSAGE_BUDGET

    assert operation.staged == operation.published == []


def test_large_bank_admission_still_rejects_cancellation_before_staging(
    refresh_archive, monkeypatch
):
    operation = _large_bank_publication(refresh_archive, monkeypatch)
    operation.service.shutdown(wait=False)

    with pytest.raises(RefreshNotice) as error:
        operation.service._publish(operation.packets, OWNER, operation.baseline)
    assert error.value.issue == RefreshIssue.INTERRUPTED

    assert operation.staged == operation.published == []


@pytest.mark.parametrize("phase", ("before_staging", "after_staging"))
def test_large_bank_admission_retains_source_identity_checks(
    refresh_archive, monkeypatch, phase
):
    operation = _large_bank_publication(refresh_archive, monkeypatch)
    changed = {"history.sqlite3": {"size": -1}}
    if phase == "before_staging":
        monkeypatch.setattr(bank, "_inputs", lambda _history: changed)
    else:
        operation.receipt["input_identities"] = changed

    with pytest.raises(RefreshError, match="archive changed"):
        operation.service._publish(operation.packets, OWNER, operation.baseline)

    assert bool(operation.staged) is (phase == "after_staging")
    assert operation.published == []
    assert not list(operation.history.parent.glob(".mac-refresh-*"))


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


def test_group_refresh_retains_member_identity_and_literal_body(group_refresh_archive):
    root, _history, session, records = group_refresh_archive
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


def test_group_refresh_publishes_closed_membership_roles(group_refresh_archive):
    root, history, session, records = group_refresh_archive
    raw = _system_link_template(
        "$username$ invited $names$ to the group chat.",
        _system_profile_link("username", [(OWNER, "Inviter")])
        + _system_profile_link("names", [(MEMBER, "Invitee")]),
    )
    scope = projection.resolve_scope(root, session)
    native = _database(scope, [*records, {
        "local_id": 5,
        "server_id": 105,
        "local_type": 10002,
        "create_time": 1_700_000_102,
        "real_sender_id": 3,
        "message_content": raw,
        "WCDB_CT_message_content": 0,
    }])
    try:
        packet, _counts = projection.prepare_packet(root, scope, native)
        expected = {
            "kind": "joined",
            "method": "invited",
            "actor_label": "Inviter",
            "subject_ids": [MEMBER],
            "viewer_subject": False,
        }
        notice = packet["messages"][-1]
        assert notice["text"] == "Inviter invited Invitee to the group chat."
        assert notice["native_text"]["system_membership"] == expected
        candidate = history.parent / "synthetic-membership-candidate.sqlite3"
        receipt = bank.stage_mac_recovery_bank(root, [packet], candidate, account_ids=[OWNER])
        bank.publish_mac_recovery_bank(root, candidate, receipt)
        with sqlite3.connect(bank.mac_recovery_bank_path(root)) as connection:
            proof = json.loads(connection.execute(
                "SELECT provenance FROM messages WHERE ordinal=5"
            ).fetchone()[0])
        assert proof["system_membership"] == expected
        repeated, counts = projection.prepare_packet(root, scope, native)
        assert repeated is None and counts["existing"] == len(records) + 1

        notice["native_text"]["system_membership"] = {**expected, "method": "qr"}
        with pytest.raises(ValueError, match="membership evidence conflicts"):
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

    def unavailable(value, marker, **options):
        if value == "New literal message":
            raise projection.WCDBDecodeError("Synthetic unavailable dictionary")
        return decode(value, marker, **options)

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


@pytest.mark.parametrize("owner_row", [17, 43])
def test_unique_nonstandard_owner_row_retains_exact_local_anchor_contract(
    refresh_archive, owner_row
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    native = _database(scope, records)
    table = projection._table(scope.peer_id)
    native.connection.execute("UPDATE Name2Id SET rowid=? WHERE rowid=2", (owner_row,))
    native.connection.execute(
        "INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(2,?,1)",
        ("wxid_unrelated_native_row",),
    )
    native.connection.execute(
        'UPDATE "' + table + '" SET real_sender_id=? WHERE real_sender_id=2',
        (owner_row,),
    )
    before = history.read_bytes()
    try:
        packet, counts = projection.prepare_packet(root, scope, native)
        assert counts == {"total": 4, "existing": 2, "excluded": 0}
        assert len(packet["anchors"]) == 2 and "account_binding" not in packet
        sent = next(row for row in packet["messages"] if row["direction"] == "sent")
        assert sent["sender_id"] == OWNER and sent["real_sender_id"] == owner_row
        assert sent["sender_proof"]["row"]["rowid"] == owner_row
        bank._validate_packet(packet, (OWNER,))
        candidate = history.parent / "dynamic-local-owner.sqlite3"
        receipt = bank.stage_mac_recovery_bank(
            root, [packet], candidate, account_ids=[OWNER]
        )
        bank.publish_mac_recovery_bank(root, candidate, receipt)
        repeated, counts = projection.prepare_packet(root, scope, native)
        assert repeated is None and counts["existing"] == 4
        assert history.read_bytes() == before
    finally:
        native.close()


@pytest.mark.parametrize("change", ["missing", "duplicate", "zero_row"])
def test_missing_or_ambiguous_native_owner_fails_before_target_projection(
    refresh_archive, change
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    native = _database(scope, records, include_target=False)
    if change == "missing":
        native.connection.execute("DELETE FROM Name2Id WHERE rowid=2")
    elif change == "duplicate":
        native.connection.execute(
            "INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(17,?,0)",
            (OWNER,),
        )
    else:
        native.connection.execute("UPDATE Name2Id SET rowid=0 WHERE rowid=2")
    try:
        with pytest.raises(RefreshError, match="owner"):
            projection.prepare_packet(root, scope, native)
    finally:
        native.close()


@pytest.mark.parametrize("selected", [False, True])
def test_refresh_validates_selected_media_proofs_without_projecting_other_chats(
    refresh_archive, selected
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    entry_session = session if selected else "wechat:" + "0" * 64
    history.with_name("media_placeholders.json").write_text(
        json.dumps(
            {"schema_version": 1, "entries": [{"conversation_id": entry_session}]}
        )
    )
    native = _database(scope, records)
    try:
        if selected:
            with pytest.raises(WechatTimelineProjectionError):
                projection.prepare_packet(root, scope, native)
        else:
            packet, counts = projection.prepare_packet(root, scope, native)
            assert packet is not None and counts["existing"] == 2
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
        with pytest.raises(
            SourceAccountBindingUnavailable, match="independently revalidated"
        ):
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
    assert "member_snapshot" not in state
    assert history.read_bytes() == original
    assert not list(history.parent.glob(".mac-refresh-*"))
    assert service.status("not-a-token") is None
    service.shutdown()


@pytest.mark.parametrize(("roster_outcome", "message_keys"), [
    ("saved", True),
    ("unavailable", True),
    ("failed", True),
    ("saved", False),
])
def test_group_refresh_api_updates_member_snapshot_independently_of_new_messages(
    group_refresh_archive, monkeypatch, roster_outcome, message_keys,
):
    root, history, session, records = group_refresh_archive
    repository = WechatGroupRosterRepository(root)
    repository.save(snapshot(account=OWNER, peer=ROOM))
    original_roster = repository.path.read_bytes()
    original_archive = history.read_bytes()
    registry = Registry(reference_count=int(message_keys))
    scope = projection.resolve_scope(root, session)
    calls = []

    def capture(owner, peer, *, registry, check_cancel):
        check_cancel()
        calls.append((owner, peer, registry))
        if roster_outcome == "failed":
            raise RefreshError("Cannot open /private/native/contact.db with a synthetic-secret.")
        return snapshot((MEMBER, "Current member"), account=owner, peer=peer, observed_at=LATE)

    monkeypatch.setattr(roster_source, "capture_group_roster", capture)
    monkeypatch.setattr(
        roster_source, "roster_source_available",
        lambda owner, **_kwargs: owner == OWNER and roster_outcome != "unavailable",
    )
    monkeypatch.setattr(
        refresh, "read_database", lambda *_args, **_kwargs: _database(scope, records[:2]),
    )
    service = refresh.WechatMacRefreshService(root, key_registry=registry, platform="darwin")
    app = Flask(__name__)
    app.extensions[WECHAT_REFRESH_EXTENSION] = service
    blueprint = Blueprint("local_resources", __name__)
    register_wechat_refresh_routes(blueprint)
    app.register_blueprint(blueprint)

    try:
        with app.test_client() as client:
            started = client.post("/api/browser/wechat/refresh", json={"session_id": session})
            assert started.status_code == 202
            status_url = started.json["status_url"]
            _wait(service, status_url.rsplit("/", 1)[1])
            response = client.get(status_url)
            assert response.status_code == 200
            result = response.json
    finally:
        service.shutdown()

    assert result["member_snapshot"] == roster_outcome
    assert result["added"] == 0
    assert result["state"] == ("succeeded" if message_keys else "failed")
    if message_keys:
        assert (result["checked"], result["total"]) == (2, 2)
    assert b"/private/native/contact.db" not in response.data
    assert b"synthetic-secret" not in response.data
    assert calls == ([] if roster_outcome == "unavailable" else [(OWNER, ROOM, registry)])
    projection_result = repository.project(OWNER, ROOM)
    if roster_outcome == "saved":
        assert projection_result["snapshot_count"] == 2
        assert projection_result["current"]["observed_at"] == LATE
    else:
        assert projection_result["snapshot_count"] == 1
        assert repository.path.read_bytes() == original_roster
    assert history.read_bytes() == original_archive


@pytest.mark.parametrize("last_shard", ["valid", "unavailable"])
def test_sparse_first_shard_does_not_block_independently_bound_later_sources(
    refresh_archive, monkeypatch, last_shard
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    service = refresh.WechatMacRefreshService(
        root, key_registry=Registry(reference_count=2), platform="darwin"
    )
    snapshots, closed = [], []
    sparse = records[:1] + records[2:]

    def reader(_reference, **_kwargs):
        index = len(snapshots)
        rows = records if index == 1 and last_shard == "valid" else sparse
        database = _database(scope, rows)
        snapshots.append(database)
        original_close = database.close

        def close_snapshot():
            closed.append(index)
            original_close()

        database.close = close_snapshot
        return database

    monkeypatch.setattr(refresh, "read_database", reader)
    before = history.read_bytes()
    state = _wait(service, service.start(session)["task_id"])
    assert state["state"] == "succeeded" and state["partial"]
    assert len(snapshots) == 2 and closed == [0, 1]
    if last_shard == "valid":
        assert (state["added"], state["total"], state["checked"]) == (2, 4, 4)
        assert bank.mac_recovery_bank_path(root).exists()
    else:
        assert state["added"] == 0 and state["total"] == 2
        assert not bank.mac_recovery_bank_path(root).exists()
    assert history.read_bytes() == before
    assert not list(history.parent.glob(".mac-refresh-*"))
    service.shutdown()


@pytest.mark.parametrize(
    "defect", ["crypto", "timeline", "cancel", "config", "owner", "drift", "proof"]
)
def test_other_source_failures_still_abort_all_publication(
    refresh_archive, monkeypatch, defect
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    service = refresh.WechatMacRefreshService(
        root, key_registry=Registry(reference_count=2), platform="darwin"
    )
    reads, closed = [], []

    def reader(_reference, **_kwargs):
        index = len(reads)
        reads.append(index)
        if index == 1 and defect == "crypto":
            raise cipher.RecoveryError("Synthetic content authentication failure")
        database = _database(scope, records)
        if index == 1 and defect == "owner":
            database.connection.execute(
                "UPDATE Name2Id SET user_name='wxid_wrong_owner' WHERE rowid=2"
            )
        original_close = database.close

        def close_snapshot():
            closed.append(index)
            original_close()

        database.close = close_snapshot
        return database

    def prepare(*args, **kwargs):
        if len(reads) == 2:
            if defect == "timeline":
                raise WechatTimelineProjectionError("invalid")
            if defect == "cancel":
                service.shutdown(wait=False)
                kwargs["check_cancel"]()
            if defect == "config":
                raise RefreshError("Synthetic source configuration drift")
            if defect == "proof":
                raise ValueError("Synthetic malformed account proof")
        result = projection.prepare_packet(*args, **kwargs)
        if len(reads) == 2 and defect == "drift":
            history.touch()
        return result

    monkeypatch.setattr(refresh, "read_database", reader)
    monkeypatch.setattr(refresh, "prepare_packet", prepare)
    before = history.read_bytes()
    state = _wait(service, service.start(session)["task_id"])
    assert state["state"] == "failed" and state["added"] == 0
    assert reads == [0, 1]
    assert closed == ([0] if defect == "crypto" else [0, 1])
    assert history.read_bytes() == before
    assert not bank.mac_recovery_bank_path(root).exists()
    assert not list(history.parent.glob(".mac-refresh-*"))
    service.shutdown()


def test_cancellation_after_an_unavailable_binding_cannot_report_success(
    refresh_archive, monkeypatch
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    service = refresh.WechatMacRefreshService(
        root, key_registry=Registry(), platform="darwin"
    )
    monkeypatch.setattr(
        refresh,
        "read_database",
        lambda *_args, **_kwargs: _database(scope, records[:1] + records[2:]),
    )

    def prepare(*args, **kwargs):
        try:
            return projection.prepare_packet(*args, **kwargs)
        except SourceAccountBindingUnavailable:
            service.shutdown(wait=False)
            raise

    monkeypatch.setattr(refresh, "prepare_packet", prepare)
    state = _wait(service, service.start(session)["task_id"])
    assert state["state"] == "failed" and state["partial"]
    assert not bank.mac_recovery_bank_path(root).exists()


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
    assert state["message"] == RefreshIssue.ARCHIVE_CHANGED.value


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

    def delegate(store, requested, cancelled, *, on_progress):
        received.append((store, requested, cancelled))
        on_progress({"phase": "reading", "parts_total": 2, "part_current": 1})
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


def test_progress_reports_real_counts_without_claiming_uncommitted_additions(
    refresh_archive, monkeypatch
):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    service = refresh.WechatMacRefreshService(
        root, key_registry=Registry(missing=3), platform="darwin"
    )
    before_publish, release = Event(), Event()
    publish = bank.publish_mac_recovery_bank

    def wait_before_publish(*args, **kwargs):
        before_publish.set()
        assert release.wait(5)
        return publish(*args, **kwargs)

    monkeypatch.setattr(
        refresh, "read_database", lambda *_args, **_kwargs: _database(scope, records)
    )
    monkeypatch.setattr(bank, "publish_mac_recovery_bank", wait_before_publish)
    token = service.start(session)["task_id"]
    try:
        assert before_publish.wait(3)
        state = service.status(token)
        assert state["state"] == "running" and state["phase"] == "saving"
        assert (
            state["parts_total"],
            state["parts_completed"],
            state["part_current"],
        ) == (1, 1, 1)
        assert (state["checked"], state["candidates"], state["unavailable_parts"]) == (
            4,
            2,
            3,
        )
        assert state["added"] == state["upgraded"] == 0
        assert not bank.mac_recovery_bank_path(root).exists()
    finally:
        release.set()
    state = _wait(service, token)
    assert state["phase"] == "complete" and state["state"] == "succeeded"
    assert (state["added"], state["total"], state["upgraded"]) == (2, 4, 0)


def test_completed_progress_reports_actual_placeholder_restorations(
    app_stubs, monkeypatch
):
    fixture, packet = app_stubs, app_stubs.packets[0]
    service = refresh.WechatMacRefreshService(
        fixture.root, key_registry=Registry(), platform="darwin"
    )
    monkeypatch.setattr(
        refresh,
        "read_database",
        lambda *_args, **_kwargs: AuthenticatedDatabase(
            sqlite3.connect(":memory:"), packet["source"], OWNER, False
        ),
    )
    monkeypatch.setattr(
        refresh,
        "prepare_packet",
        lambda *_args, **_kwargs: (packet, {"total": 1, "existing": 0, "excluded": 0}),
    )
    state = _wait(service, service.start(packet["session_id"])["task_id"])
    assert state["state"] == "succeeded" and state["phase"] == "complete"
    assert (state["added"], state["upgraded"], state["candidates"]) == (0, 1, 1)


def test_older_host_receives_live_progress_while_worker_is_still_running(
    refresh_archive, monkeypatch
):
    from app.core import wechat_mac_refresh_worker

    root, _history, session, _records = refresh_archive
    registry = Registry()
    service = refresh.WechatMacRefreshService(
        root, key_registry=registry, platform="darwin"
    )
    monkeypatch.setattr(refresh, "sys", SimpleNamespace(version_info=(3, 13)))
    reached, release = Event(), Event()

    def delegate(*_args, on_progress):
        on_progress(
            {
                "phase": "comparing",
                "parts_total": 7,
                "part_current": 2,
                "parts_completed": 1,
                "checked": 1500,
                "candidates": 3,
            }
        )
        reached.set()
        assert release.wait(5)
        return {
            "state": "succeeded",
            "message": "Chat history refreshed.",
            "added": 0,
            "total": 2,
            "partial": False,
            "checked": 1500,
            "phase": "complete",
        }

    monkeypatch.setattr(wechat_mac_refresh_worker, "run_compatible_worker", delegate)
    token = service.start(session)["task_id"]
    try:
        assert reached.wait(3)
        state = service.status(token)
        assert state["state"] == "running" and state["phase"] == "comparing"
        assert (state["checked"], state["candidates"]) == (1500, 3)
        assert registry.loaded_keys == 0
    finally:
        release.set()
    assert _wait(service, token)["state"] == "succeeded"


@pytest.mark.parametrize("owner_row", [2, 17])
def test_global_binding_revalidates_only_known_coordinate_witnesses(
    refresh_archive, owner_row
):
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
        # Neither dimension of the shard identity may consume the proof lookup budget.
        for name, source_overrides, binding_overrides in (
            (
                "different-shard",
                {"database_relative_path": "db_storage/message/message_99.db"},
                {"source": {**binding["source"], "database_relative_path": "db_storage/message/message_99.db"}},
            ),
            (
                "different-directory",
                {"account_directory": OWNER + "_ef01"},
                {"account_directory": OWNER + "_ef01"},
            ),
        ):
            wrong_scope = json.dumps({
                **packet,
                "source": {**source, **source_overrides},
                "account_binding": {**binding, **binding_overrides},
            })
            rows = [(f"{name}-{number:02d}", wrong_scope) for number in range(32)]
            stored.executemany(
                "INSERT INTO sources VALUES(?,?,?)",
                [(proof_id, OWNER, proof) for proof_id, proof in rows],
            )
            stored.executemany(
                "INSERT INTO conversation_proofs VALUES(?,?,?)",
                [(session, proof_id, proof) for proof_id, proof in rows],
            )
        stored.execute(
            "INSERT INTO sources VALUES('proof',?,?)", (OWNER, json.dumps(packet))
        )
        stored.execute(
            "INSERT INTO conversation_proofs VALUES(?,'proof',?)",
            (other_session, json.dumps(packet)),
        )
    if owner_row != 2:
        native.connection.execute(
            "UPDATE Name2Id SET rowid=? WHERE rowid=2", (owner_row,)
        )
        native.connection.execute(
            "INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(2,?,1)",
            ("wxid_unrelated_native_row",),
        )
        for current_table in (projection._table(PEER), table):
            native.connection.execute(
                'UPDATE "'
                + current_table
                + '" SET real_sender_id=? WHERE real_sender_id=2',
                (owner_row,),
            )
        names = {
            row["rowid"]: dict(row)
            for row in native.connection.execute(
                "SELECT rowid,user_name,is_session FROM Name2Id"
            )
        }
        peers = {row["user_name"]: row for row in names.values()}
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
        assert result["schema_version"] == 2
        assert result["owner_proof"]["row"]["rowid"] == owner_row
        assert {anchor["peer_id"] for anchor in result["anchors"]} == {PEER, other}
        assert any(anchor["direction"] == "sent" for anchor in result["anchors"])
        assert all(
            anchor["real_sender_id"] == owner_row
            and anchor["sender_proof"] == result["owner_proof"]
            for anchor in result["anchors"]
            if anchor["direction"] == "sent"
        )
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
