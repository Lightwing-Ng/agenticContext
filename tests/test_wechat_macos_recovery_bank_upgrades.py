"""Monotone App-stub shadows retain source files, native identity and stable anchors.

Code version: v1.0.2-codex.0
"""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_archive import (
    WechatArchiveRepository,
    WechatTimelineProjectionError,
    _file_identity,
)
from app.core.wechat_history_browser import query_wechat_history
from app.core.wechat_resource_index import build_resource_index, resource_index_path
from app.core.wechat_session_export import (
    WechatSessionExport,
    WechatSessionSourceChanged,
)
from tests import test_wechat_macos_recovery_bank as bank_fixtures


mac_archive = bank_fixtures.mac_archive
bank_archive = bank_fixtures.bank_archive
OWNER = bank_fixtures.OWNER
PEER = bank_fixtures.PEER
MEMBER = bank_fixtures.MEMBER


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@pytest.fixture
def app_stubs(bank_archive, tmp_path):
    fixture = bank_archive
    direct, group = deepcopy(fixture.direct), deepcopy(fixture.group)
    direct["messages"] = [direct["messages"][-1]]
    payment = bank_fixtures._message(
        57,
        49,
        raw="<msg><appmsg><type>2000</type><title>Synthetic transfer card</title>"
        "<wcpayinfo><feedesc>￥5.20</feedesc><pay_memo>Literal memo</pay_memo>"
        "</wcpayinfo></appmsg></msg>",
        sender=MEMBER,
    )
    group["messages"] = [group["messages"][3], payment]
    snapshot = tmp_path / "preserved/snapshot"
    database_root = (
        snapshot
        / "AppDomain-com.tencent.xin/Documents"
        / hashlib.md5(OWNER.encode(), usedforsecurity=False).hexdigest()
        / "DB"
    )
    database_root.mkdir(parents=True)
    (snapshot.parent / "preservation-manifest.json").write_text(
        json.dumps({"snapshot_id": "b" * 64, "selected_files": []})
    )
    source = database_root / "message_1.sqlite"
    with closing(sqlite3.connect(source)) as connection, connection:
        for packet in (direct, group):
            table = (
                "Chat_"
                + hashlib.md5(
                    packet["peer_id"].encode(), usedforsecurity=False
                ).hexdigest()
            )
            connection.execute(
                f'CREATE TABLE "{table}" (CreateTime INTEGER,Des INTEGER,'
                "MesLocalID INTEGER PRIMARY KEY,Message BLOB,MesSvrID INTEGER,"
                "Type INTEGER,WCDB_CT_Message INTEGER)"
            )
            connection.executemany(
                f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?)',
                [
                    (
                        message["create_time"],
                        0 if message["direction"] == "sent" else 1,
                        message["local_id"],
                        b"synthetic-opaque-app-metadata",
                        int(message["server_id"]),
                        49,
                        2,
                    )
                    for message in packet["messages"]
                ],
            )
    report = build_resource_index(fixture.root, snapshot, account_ids=(OWNER,))
    assert report["messages"] == 3
    catalog = resource_index_path(fixture.root)
    with closing(sqlite3.connect(catalog)) as connection:
        connection.row_factory = sqlite3.Row
        originals = {
            json.loads(row["provenance"])["server_id"]: dict(row)
            for row in connection.execute("SELECT * FROM messages")
        }
    return SimpleNamespace(
        fixture=fixture,
        root=fixture.root,
        history=fixture.history,
        source=source,
        snapshot=snapshot,
        catalog=catalog,
        originals=originals,
        packets=[direct, group],
        quote=direct["messages"][0],
        forward=group["messages"][0],
        payment=payment,
    )


def _publish(fixture, *, packets=None, name="upgrade"):
    return bank_fixtures._publish(
        fixture.fixture,
        packets=packets if packets is not None else fixture.packets,
        name=name,
    )


def _detail(fixture, session):
    return WechatArchiveRepository(fixture.root).query(
        session=session, persist_event_cache=False
    )


def _change_original(fixture, message, *, text=None, sender=None, proof_change=None):
    identifier = fixture.originals[message["server_id"]]["id"]
    with closing(sqlite3.connect(fixture.catalog)) as connection, connection:
        if text is not None:
            connection.execute(
                "UPDATE messages SET text=? WHERE id=?", (text, identifier)
            )
        if sender is not None:
            connection.execute(
                "UPDATE messages SET sender_id=? WHERE id=?", (sender, identifier)
            )
        if proof_change is not None:
            raw = connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (identifier,)
            ).fetchone()[0]
            proof = json.loads(raw)
            proof_change(proof)
            connection.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (_json(proof), identifier),
            )


def test_quote_forward_and_payment_shadows_keep_ids_counts_and_all_original_bytes(
    app_stubs,
):
    fixture = app_stubs
    history, catalog, source = (
        fixture.history.read_bytes(),
        fixture.catalog.read_bytes(),
        fixture.source.read_bytes(),
    )
    before = {
        packet["session_id"]: _detail(fixture, packet["session_id"])["count"]
        for packet in fixture.packets
    }
    receipt = _publish(fixture)
    assert receipt["added"] == 0 and receipt["upgraded"] == 3
    assert before == {
        fixture.packets[0]["session_id"]: 3,
        fixture.packets[1]["session_id"]: 4,
    }
    for packet in fixture.packets:
        page = _detail(fixture, packet["session_id"])
        assert (
            page["count"]
            == page["current"]["message_count"]
            == before[packet["session_id"]]
        )
        by_id = {row["id"]: row for row in page["messages"]}
        assert len(by_id) == page["count"]
        for message in packet["messages"]:
            original = fixture.originals[message["server_id"]]
            row = by_id[original["id"]]
            assert row["text"] == message["text"]
            assert row["sender_id"] == message["sender_id"]
            assert (
                json.loads(row["provenance"])["macos_recovery"]["server_id"]
                == message["server_id"]
            )
    global_page = WechatArchiveRepository(fixture.root).query(persist_event_cache=False)
    assert global_page["message_count"] == 7
    assert fixture.history.read_bytes() == history
    assert fixture.catalog.read_bytes() == catalog
    assert fixture.source.read_bytes() == source
    with closing(sqlite3.connect(fixture.fixture.bank_path)) as connection:
        connection.row_factory = sqlite3.Row
        shadows = [
            dict(row) for row in connection.execute("SELECT * FROM shadow_replacements")
        ]
    assert {row["original_id"] for row in shadows} == {
        row["id"] for row in fixture.originals.values()
    }
    for row in shadows:
        original = next(
            value
            for value in fixture.originals.values()
            if value["id"] == row["original_id"]
        )
        assert row["source_file_name"] == fixture.catalog.name
        assert row["source_file_sha256"] == hashlib.sha256(catalog).hexdigest()
        assert row["original_text_sha256"] == _sha(original["text"])
        assert row["original_provenance_sha256"] == _sha(original["provenance"])
        assert len(row["original_row_sha256"]) == 64


def test_upgrade_presentation_uses_native_quote_forward_and_payment_roles(app_stubs):
    fixture = app_stubs
    _publish(fixture)
    direct = query_wechat_history(
        fixture.root,
        session=fixture.packets[0]["session_id"],
        persist_event_cache=False,
    )
    quote_id = fixture.originals[fixture.quote["server_id"]]["id"]
    quote = next(item for item in direct.items if item.stable_id == quote_id)
    assert quote.messaging["display_text"] == fixture.quote["text"]
    assert quote.messaging["quoted_reply"]["resource"]["kind"] == "image"
    group = query_wechat_history(
        fixture.root,
        session=fixture.packets[1]["session_id"],
        persist_event_cache=False,
    )
    by_id = {item.stable_id: item for item in group.items}
    forward = by_id[fixture.originals[fixture.forward["server_id"]]["id"]]
    assert forward.messaging["forwarded_record"]["title"] == "Literal source bundle"
    assert (
        forward.messaging["forwarded_record"]["items"][0].content_text
        == "Forwarded literal text"
    )
    payment = by_id[fixture.originals[fixture.payment["server_id"]]["id"]]
    assert payment.messaging["payment"]["kind"] == "transfer"
    assert "￥5.20" in payment.content_text and "Literal memo" in payment.content_text
    assert payment.messaging["sender_id"] == MEMBER
    assert fixture.originals[fixture.payment["server_id"]]["sender_id"] in (None, "")


@pytest.mark.parametrize(
    "change",
    [
        "primary_text",
        "sender",
        "direction",
        "clock",
        "type",
        "safe_title",
        "app_subtype",
        "native_metadata",
        "locator_metadata",
        "reference_metadata",
    ],
)
def test_non_monotone_or_nonempty_originals_are_retained_as_conflicts(
    app_stubs, change
):
    fixture = app_stubs
    message = fixture.quote
    if change == "primary_text":
        _change_original(
            fixture, message, text="An independently retained primary body variant"
        )
    elif change == "sender":
        _change_original(fixture, message, sender="wxid_another_native_sender")
    elif change == "direction":
        identifier = fixture.originals[message["server_id"]]["id"]
        with closing(sqlite3.connect(fixture.catalog)) as connection, connection:
            connection.execute(
                "UPDATE messages SET direction='received' WHERE id=?", (identifier,)
            )
    else:

        def alter(proof):
            if change == "clock":
                proof["unix_timestamp"] += 1
            elif change == "type":
                proof["message_type"] = 3
            elif change == "safe_title":
                proof["resource_reference"]["safe_title"] = (
                    "Original asserted card title"
                )
            elif change == "app_subtype":
                proof["resource_reference"]["app_subtype"] = "6"
            elif change == "native_metadata":
                proof["text_components"] = [
                    {"kind": "reply_text", "text": "Original asserted copy"}
                ]
            elif change == "locator_metadata":
                proof["locator"]["text_components"] = [
                    {"kind": "reply_text", "text": "Original asserted copy"}
                ]
            else:
                proof["resource_reference"]["text_components"] = [
                    {"kind": "reply_text", "text": "Original asserted copy"}
                ]

        _change_original(fixture, message, proof_change=alter)
    catalog = fixture.catalog.read_bytes()
    receipt = _publish(fixture, packets=[fixture.packets[0]])
    assert receipt["added"] == receipt["upgraded"] == 0
    assert receipt["conflicts"] == 1
    row = next(
        row
        for row in _detail(fixture, fixture.packets[0]["session_id"])["messages"]
        if row["id"] == fixture.originals[message["server_id"]]["id"]
    )
    assert "macos_recovery" not in json.loads(row["provenance"])
    assert fixture.catalog.read_bytes() == catalog


@pytest.mark.parametrize("empty", [None, "", {}])
def test_empty_optional_metadata_does_not_block_a_monotone_upgrade(app_stubs, empty):
    fixture = app_stubs

    def alter(proof):
        proof["resource_reference"].update(safe_title=empty, app_subtype=empty)
        proof["text_components"] = deepcopy(empty)

    _change_original(fixture, fixture.quote, proof_change=alter)
    receipt = _publish(fixture, packets=[fixture.packets[0]])
    assert receipt["added"] == 0 and receipt["upgraded"] == 1


def test_shadow_replay_and_another_snapshot_do_not_add_or_duplicate_bubbles(app_stubs):
    fixture = app_stubs
    _publish(fixture)
    replay = _publish(fixture, name="upgrade-replay")
    assert replay["added"] == replay["upgraded"] == replay["occurrences_added"] == 0
    alternative = deepcopy(fixture.packets)
    for packet in alternative:
        packet["source"]["snapshot_id"] = "another-authenticated-upgrade-snapshot"
        packet["receipt_id"] += "-another-snapshot"
    receipt = _publish(fixture, packets=alternative, name="upgrade-another-snapshot")
    assert receipt["added"] == receipt["upgraded"] == 0
    assert receipt["occurrences_added"] == 3
    assert _detail(fixture, fixture.packets[0]["session_id"])["count"] == 3
    assert _detail(fixture, fixture.packets[1]["session_id"])["count"] == 4


def test_catalog_drift_blocks_staged_publication_and_published_shadow_reads(app_stubs):
    fixture = app_stubs
    candidate, receipt = bank_fixtures._stage(fixture.fixture, packets=fixture.packets)
    _change_original(fixture, fixture.quote, text="[App message] changed")
    with pytest.raises(ValueError):
        bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    assert not fixture.fixture.bank_path.exists()


def test_published_shadow_fails_closed_when_its_original_catalog_changes(app_stubs):
    fixture = app_stubs
    _publish(fixture)
    bank_bytes = fixture.fixture.bank_path.read_bytes()
    _change_original(
        fixture, fixture.quote, text="An original catalog changed after review"
    )
    result = _detail(fixture, fixture.packets[0]["session_id"])
    assert result["resource_index_status"] == "invalid"
    row = next(
        row
        for row in result["messages"]
        if row["id"] == fixture.originals[fixture.quote["server_id"]]["id"]
    )
    assert "macos_recovery" not in json.loads(row["provenance"])
    assert row["text"] == "An original catalog changed after review"
    repository = WechatArchiveRepository(fixture.root)
    with closing(sqlite3.connect(fixture.history)) as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(WechatTimelineProjectionError):
            repository._timeline_tables(
                connection, _file_identity(fixture.history), strict=True
            )
    assert fixture.fixture.bank_path.read_bytes() == bank_bytes


def test_main_archive_app_placeholders_are_never_shadowed(app_stubs):
    fixture = app_stubs
    original = fixture.originals[fixture.quote["server_id"]]
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(original.values()),
        )
        connection.execute(
            "UPDATE conversations SET message_count=message_count+1,last_sort=?,"
            "last_time=?,latest_text=? WHERE id=?",
            (
                original["time_sort"],
                original["timestamp_value"],
                original["text"],
                original["conversation_id"],
            ),
        )
    build_resource_index(fixture.root, fixture.snapshot, account_ids=(OWNER,))
    before = fixture.history.read_bytes()
    receipt = _publish(fixture, packets=[fixture.packets[0]])
    assert receipt["added"] == receipt["upgraded"] == 0
    assert receipt["conflicts"] == 1
    row = next(
        row
        for row in _detail(fixture, original["conversation_id"])["messages"]
        if row["id"] == original["id"]
    )
    assert row["text"] == "[App message]"
    assert "macos_recovery" not in json.loads(row["provenance"])
    assert fixture.history.read_bytes() == before


def test_two_visible_originals_with_one_server_id_are_not_guessed(app_stubs):
    fixture = app_stubs
    original = fixture.originals[fixture.quote["server_id"]]
    duplicate = dict(original)
    duplicate["id"] = _sha("another-independently-retained-app-occurrence")
    duplicate["ordinal"] = 999
    proof = json.loads(duplicate["provenance"])
    proof["record_id"] = duplicate["id"]
    proof["locator"].update(local_id=999, row_id=999)
    duplicate["provenance"] = _json(proof)
    with closing(sqlite3.connect(fixture.catalog)) as connection, connection:
        connection.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(duplicate.values()),
        )
        connection.execute(
            "UPDATE conversations SET message_count=message_count+1 WHERE id=?",
            (original["conversation_id"],),
        )
    before = _detail(fixture, original["conversation_id"])["count"]
    receipt = _publish(fixture, packets=[fixture.packets[0]])
    assert receipt["added"] == receipt["upgraded"] == 0
    assert receipt["conflicts"] == 1
    page = _detail(fixture, original["conversation_id"])
    assert page["count"] == before
    assert all(
        "macos_recovery" not in json.loads(row["provenance"])
        for row in page["messages"]
    )


def _freeze_v1_bank(path):
    """Serialize the prior closed schema as a compatibility fixture."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE shadow_replacements")
        connection.execute(
            "UPDATE archive_info SET value='1' WHERE key='schema_version'"
        )
        connection.execute(
            "UPDATE archive_info SET value='v1.0.0-codex.0' WHERE key='code_version'"
        )
        digest = hashlib.sha256()
        for table in (
            "sources",
            "conversation_proofs",
            "event_keys",
            "occurrences",
            "gaps",
            "messages",
            "conversations",
        ):
            digest.update(table.encode())
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1,2"):
                digest.update(_json(tuple(row)).encode())
                digest.update(b"\n")
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='payload_sha256'",
            (digest.hexdigest(),),
        )


def test_v1_bank_migrates_only_in_candidate_and_preserves_existing_event(app_stubs):
    fixture = app_stubs
    existing_packet = deepcopy(fixture.fixture.group)
    existing_packet["messages"] = [existing_packet["messages"][0]]
    _publish(fixture, packets=[existing_packet], name="initial-legacy-event")
    _freeze_v1_bank(fixture.fixture.bank_path)
    old_bank = fixture.fixture.bank_path.read_bytes()
    before_history, before_catalog = (
        fixture.history.read_bytes(),
        fixture.catalog.read_bytes(),
    )
    before = _detail(fixture, fixture.packets[1]["session_id"])
    assert before["count"] == 5
    old_event = next(
        row
        for row in before["messages"]
        if row["text"] == existing_packet["messages"][0]["text"]
    )
    candidate, receipt = bank_fixtures._stage(
        fixture.fixture, packets=fixture.packets, name="migrated-candidate"
    )
    assert receipt["added"] == 0 and receipt["upgraded"] == 3
    assert fixture.fixture.bank_path.read_bytes() == old_bank
    with closing(sqlite3.connect(candidate)) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM archive_info"))
        assert metadata["schema_version"] == "3"
        assert (
            connection.execute("SELECT COUNT(*) FROM shadow_replacements").fetchone()[0]
            == 3
        )
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    after = _detail(fixture, fixture.packets[1]["session_id"])
    assert after["count"] == 5
    retained = next(row for row in after["messages"] if row["id"] == old_event["id"])
    assert retained == old_event
    assert fixture.history.read_bytes() == before_history
    assert fixture.catalog.read_bytes() == before_catalog


def test_export_pins_active_fallback_and_the_distinct_original_shadow_catalog(
    app_stubs, monkeypatch
):
    fixture = app_stubs
    fallback = fixture.history.with_name("resource_revalidated.sqlite3")
    fallback.write_bytes(fixture.catalog.read_bytes())
    fallback.chmod(0o600)
    _publish(fixture)
    monkeypatch.setattr(
        "app.core.wechat_native_resource_revalidation.select_resource_index",
        lambda _history, *, conversation_id=None: (
            fallback if conversation_id else fixture.catalog
        ),
    )
    export = WechatSessionExport(
        fixture.root, fixture.packets[0]["session_id"], write_event_cache=False
    )
    databases = {
        row[1]: row[2] for row in export.connection.execute("PRAGMA database_list")
    }
    assert databases["resource_index"] == str(fallback)
    assert fallback in export.sources and fixture.catalog in export.sources
    assert export.conversation["message_count"] == 3
    _change_original(
        fixture, fixture.quote, text="Changed original under an active fallback"
    )
    with pytest.raises(WechatSessionSourceChanged):
        next(export.items())
    assert export.connection is None
