"""Native server IDs prove cross-device events while conflicts retain source rows.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3
from unittest.mock import patch

import pytest

from app.core.chat_history_browser import query_chat_history
from app.core.config import CrawlConfig
from app.core.wechat_archive import _identifier
from app.core.wechat_session_export import WechatSessionExport
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_wechat_conversation_events as fixtures
from tests.wechat_browser_markup import get_verified_browser_page


event_store = fixtures.event_store


def _insert(connection, row):
    connection.execute(
        "INSERT INTO messages("
        + ",".join(row)
        + ") VALUES("
        + ",".join("?" for _ in row)
        + ")",
        tuple(row.values()),
    )


@pytest.fixture
def native_store(event_store):
    fixture = event_store
    duplicates = []
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("DELETE FROM messages WHERE ordinal<100")
        connection.execute(
            "UPDATE conversations SET partition_id=?,message_count=6 WHERE id=?",
            (fixtures.NATIVE, fixtures.SESSION),
        )
        metadata = {
            "source_id": fixtures.NATIVE,
            "source_account_id": fixtures.OWNER,
            "account_hash_verified": True,
            "account_hash": hashlib.md5(fixtures.OWNER.encode()).hexdigest(),
            "source_label": "iPhone archive",
            "input_databases": [
                {
                    "path": "Documents/"
                    + hashlib.md5(fixtures.OWNER.encode()).hexdigest()
                    + "/DB/message.sqlite",
                    "sha256": "a" * 64,
                    "file_id": "b" * 40,
                    "bytes": 4096,
                }
            ],
        }
        connection.execute(
            "INSERT INTO source_snapshots VALUES(3,?,?,?)",
            ("iphone-snapshot", fixtures.NATIVE, json.dumps(metadata)),
        )
        for index, (_, native) in enumerate(fixture.ids):
            row = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (native,)
                ).fetchone()
            )
            proof = json.loads(row["provenance"])
            proof.update(record_id="iphone-record-" + str(index), source_snapshot=3)
            proof["locator"].pop("database_relative_path")
            proof["locator"].update(
                text_origin="native_wcdb_message_cell", local_id=900 + index
            )
            clock = datetime.fromisoformat(row["timestamp_value"]) + timedelta(
                seconds=1
            )
            proof["unix_timestamp"] = int(clock.timestamp())
            identifier = _identifier(fixtures.NATIVE, proof["record_id"])
            row.update(
                id=identifier,
                ordinal=200 + index,
                sender_label="An older source label",
                timestamp_value=clock.isoformat(),
                timestamp_label=clock.strftime("%d %b %Y %H:%M:%S"),
                time_sort=clock.timestamp(),
                provenance=json.dumps(proof),
            )
            _insert(connection, row)
            duplicates.append(identifier)
    fixture.duplicate_ids = duplicates
    fixture.native_ids = [pair[1] for pair in fixture.ids]
    return fixture


def test_verified_server_events_ignore_clock_and_device_local_id_differences(
    native_store,
):
    fixture = native_store
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    connection, result = fixtures._run(fixture)
    connection.close()
    assert result["status"] == "ready"
    assert result["original_count"] == 6
    assert result["display_count"] == result["count_delta"] == 3
    assert result["preferred_by_id"] == dict(
        zip(fixture.duplicate_ids, fixture.native_ids)
    )
    for identifier in fixture.native_ids:
        members = result["members_by_preferred"][identifier]
        assert len(members) == 2
        assert {item["source_snapshot_id"] for item in members} == {
            "ipad-snapshot",
            "iphone-snapshot",
        }
        assert len({item["source_clock"] for item in members}) == 2
        assert all(item["clock_disagreement"] for item in members)
        assert members[0]["source_label"] == "iPad archive"
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )


@pytest.mark.parametrize(
    "damage,expected",
    [
        ("different_server", 2),
        ("zero_server", 2),
        ("float_server", 2),
        ("different_body", 2),
        ("same_snapshot", 2),
        ("wrong_peer", 2),
        ("forged_id", 2),
        ("unknown_sender", 2),
        ("different_direction", 2),
        ("unknown_extraction", 2),
        ("different_type", 2),
        ("invalid_clock", 2),
        ("third_conflicting_body", 2),
        ("third_unknown", 2),
        ("same_source_repeat", 2),
        ("wrong_owner", 0),
        ("unverified_hash", 0),
        ("missing_database_receipt", 0),
        ("ambiguous_database_receipt", 0),
        ("wrong_database_path", 0),
    ],
)
def test_ambiguous_or_conflicting_native_observations_stay_separate(
    native_store, damage, expected
):
    fixture = native_store
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.duplicate_ids[0],)
            ).fetchone()
        )
        proof = json.loads(row["provenance"])
        metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=3"
            ).fetchone()[0]
        )
        if damage == "different_server":
            proof["server_id"] = "700000001"
        elif damage == "zero_server":
            proof["server_id"] = "0"
        elif damage == "float_server":
            proof["server_id"] = 9.223372036854776e18
        elif damage == "different_body":
            row["text"] += " different"
        elif damage == "same_snapshot":
            proof["source_snapshot"] = 2
        elif damage == "wrong_peer":
            proof["locator"]["table"] = "Chat_wrong_peer"
        elif damage == "forged_id":
            row["id"] = hashlib.sha256(b"forged").hexdigest()
        elif damage == "unknown_sender":
            row["sender_id"] = ""
        elif damage == "different_direction":
            row["direction"] = "sent"
        elif damage == "unknown_extraction":
            proof["text_extraction_kind"] = "unresolved"
        elif damage == "different_type":
            proof["message_type"] = 10000
        elif damage == "invalid_clock":
            proof["unix_timestamp"] += 1
        elif damage == "wrong_owner":
            metadata["source_account_id"] = "wxid_another_owner"
        elif damage == "unverified_hash":
            metadata["account_hash_verified"] = False
        elif damage == "missing_database_receipt":
            metadata.pop("input_databases")
        elif damage == "ambiguous_database_receipt":
            metadata["input_databases"] *= 2
        elif damage == "wrong_database_path":
            metadata["input_databases"][0]["path"] = "Documents/wrong/DB/message.sqlite"
        elif damage in {
            "third_conflicting_body",
            "third_unknown",
            "same_source_repeat",
        }:
            extra = deepcopy(row)
            extra_proof = deepcopy(proof)
            extra_proof["record_id"] = "third-observation"
            if damage != "same_source_repeat":
                connection.execute(
                    "INSERT INTO source_snapshots VALUES(4,?,?,?)",
                    ("third-snapshot", fixtures.NATIVE, json.dumps(metadata)),
                )
                extra_proof["source_snapshot"] = 4
            if damage == "third_conflicting_body":
                extra["text"] += " conflicting"
            elif damage == "third_unknown":
                extra_proof["text_extraction_kind"] = "unresolved"
            extra.update(
                id=_identifier(fixtures.NATIVE, extra_proof["record_id"]),
                ordinal=300,
                provenance=json.dumps(extra_proof),
            )
            _insert(connection, extra)
        connection.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=3", (json.dumps(metadata),)
        )
        connection.execute(
            "DELETE FROM messages WHERE id=?", (fixture.duplicate_ids[0],)
        )
        row["provenance"] = json.dumps(proof)
        _insert(connection, row)
    connection, result = fixtures._run(fixture)
    connection.close()
    assert result["status"] == "ready"
    assert result["count_delta"] == expected
    assert fixture.duplicate_ids[0] not in result["preferred_by_id"]


def test_quote_reference_semantics_must_match_exactly(native_store):
    fixture = native_store
    with sqlite3.connect(fixture.repository.path) as connection:
        for identifier in (fixture.native_ids[0], fixture.duplicate_ids[0]):
            proof = json.loads(
                connection.execute(
                    "SELECT provenance FROM messages WHERE id=?", (identifier,)
                ).fetchone()[0]
            )
            reference = {
                "original_message_type": 1,
                "source_sender_id": fixtures.PEER,
                "source_sender_label": "Source label",
                "source_chat_id": fixtures.OWNER,
                "source_ids": {"svrid": "800000001"},
            }
            proof.update(
                message_type=49,
                appmsg_subtype=57,
                text_extraction_kind="quoted_reply",
                quoted_reference=deepcopy(reference),
                text_components=[
                    {"kind": "reply_text", "text": "Reply"},
                    {"kind": "quoted_text", "text": "Original", **reference},
                ],
            )
            connection.execute(
                "UPDATE messages SET text=?,provenance=? WHERE id=?",
                ("Reply\n\nOriginal", json.dumps(proof), identifier),
            )
    connection, first = fixtures._run(fixture)
    connection.close()
    assert first["count_delta"] == 3
    with sqlite3.connect(fixture.repository.path) as connection:
        proof["quoted_reference"]["source_chat_id"] = "wxid_different_chat"
        proof["text_components"][1]["source_chat_id"] = "wxid_different_chat"
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(proof), fixture.duplicate_ids[0]),
        )
    connection, second = fixtures._run(fixture)
    connection.close()
    assert second["count_delta"] == 2


def test_counts_search_source_jumps_and_csv_share_the_same_native_projection(
    native_store,
):
    fixture = native_store
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.execute(
            "UPDATE messages SET search_text=search_text||' source-only-alias' WHERE id=?",
            (fixture.duplicate_ids[0],),
        )
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    page = query_chat_history(fixture.store, source="wechat", session=fixtures.SESSION)
    assert page.total_count == page.current_session.message_count == 3
    assert [item.stable_id for item in page.items] == fixture.native_ids
    assert all(len(item.messaging["source_observations"]) == 2 for item in page.items)
    found = query_chat_history(
        fixture.store,
        source="wechat",
        session=fixtures.SESSION,
        query="source-only-alias",
    )
    assert found.total_count == 1 and found.items[0].stable_id == fixture.native_ids[0]
    listing = query_chat_history(fixture.store, source="wechat")
    assert listing.sessions[0].message_count == 3
    export = WechatSessionExport(fixture.store, fixtures.SESSION)
    assert export.conversation["message_count"] == 3
    assert [row["id"] for row in export.items()] == fixture.native_ids
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )


def test_native_warm_cache_never_rescans_source_rows(native_store, monkeypatch):
    import app.core.wechat_native_server_events as native_events

    connection, first = fixtures._run(native_store)
    connection.close()
    assert first["count_delta"] == 3

    def unexpected_scan(*args, **kwargs):
        raise AssertionError("Warm native projection scanned raw messages.")

    monkeypatch.setattr(native_events, "project_native_server_events", unexpected_scan)
    connection, second = fixtures._run(native_store)
    connection.close()
    assert second["preferred_by_id"] == first["preferred_by_id"]


def test_disclosure_renders_preserved_source_count_and_both_uncorrected_clocks(
    native_store, tmp_path
):
    fixture = native_store
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            fixture.store,
            computer_use_settings_path=tmp_path / "agent.json",
            computer_use_runtime_root=tmp_path / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    response = get_verified_browser_page(
        app.test_client(),
        "/browser",
        query_string={
            "view": "text",
            "source": "wechat",
            "session": fixtures.SESSION,
            "account": "account:" + fixtures.OWNER,
            "chat_kind": "direct",
        },
    )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert (
        html.count('class="browser-chat-message wechat-message wechat-message--') == 3
    )
    assert html.count("2 preserved observations") == 3
    assert (
        html.count("Source clocks differ; each device observation is preserved.") == 3
    )
    assert 'datetime="2020-01-01T08:00:00+08:00"' in html
    assert 'datetime="2020-01-01T08:00:01+08:00"' in html
    assert all(
        'id="wechat_message_' + identifier + '" data-wechat-source-alias' in html
        for identifier in fixture.duplicate_ids
    )
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )
