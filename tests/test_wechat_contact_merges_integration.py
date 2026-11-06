"""Explicit person associations aggregate views without rewriting native identities.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_mcp_archive as archive_queries
from app.core.config import CrawlConfig
from app.core.wechat_archive import _identifier, wechat_archive_path
from app.core.wechat_contacts import contact_registry_path, initialize_contact_registry
from app.core.wechat_display_names import display_names_path
from app.core.wechat_text import native_components_text
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests.test_wechat_contacts import bind, insert_contact
from tests.test_wechat_reconcile import merge, record, source


FIRST = "wxid_association_first"
SECOND = "wxid_association_second"
NAMESAKE = "wxid_association_namesake"
ROOM_A = "12345678901@chatroom"
ROOM_B = "12345678902@chatroom"
UNKNOWN = "Union unknown original statement."
REPEATED = "A genuinely repeated statement."
START = datetime(2000, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=8)))


def _association(fixture, *, enabled=True):
    association = {
        "target_contact_id": fixture.target,
        "member_contact_ids": [fixture.target, fixture.member],
        "authority": "user_confirmed_same_person",
        "evidence_id": hashlib.sha256(b"Synthetic explicit user confirmation").hexdigest(),
        "confirmed_at": "2000-01-03T00:00:00+08:00",
        "confirmation": "The user explicitly confirmed both synthetic identities belong to one person.",
    }
    fixture.associations.write_text(
        json.dumps(
            {"schema_version": 1, "associations": [association] if enabled else []}
        ),
        encoding="utf-8",
    )


def _preserved(fixture):
    return fixture.history.read_bytes(), fixture.registry.read_bytes()


def _csv_rows(body):
    assert body.startswith(b"\xef\xbb\xbf")
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline=""))
    assert reader.fieldnames == ["Time", "Statement"]
    return list(reader)


@pytest.fixture
def associated_store(tmp_path):
    first, second = source("wxid_viewer_first"), source("wxid_viewer_second")
    observations = []
    expected, timestamps = {}, {}
    specifications = (
        (first, "first-known", FIRST, ROOM_A, 0, "Union first identity statement."),
        (second, "second-known", SECOND, ROOM_B, 1, "Union second identity statement."),
        (first, "first-private", FIRST, "wxid_private_peer", 2, "Union private statement."),
        (second, "repeat-one", SECOND, ROOM_B, 3, REPEATED),
        (second, "repeat-two", SECOND, ROOM_B, 4, REPEATED),
        (second, "namesake", NAMESAKE, ROOM_B, 5, "Namesake must remain independent."),
    )
    for number, (metadata, name, sender, peer, seconds, text) in enumerate(
        specifications, 1
    ):
        stamp = START + timedelta(seconds=seconds)
        observations.append(
            record(
                metadata,
                name,
                peer=peer,
                sender_id=sender,
                sender_display_name="Identical historical nickname",
                text=text,
                sent_at_utc_iso=stamp.isoformat(),
                sent_at_raw=str(int(stamp.timestamp())),
                unix_timestamp=int(stamp.timestamp()),
                server_id=str(9_000_000_000_000_000_000 + number),
            )
        )
        if sender != NAMESAKE:
            identifier = _identifier(metadata["source_id"], name)
            expected[identifier] = sender
            timestamps[identifier] = stamp.isoformat()
    quoted = {
        "kind": "quoted_text",
        "text": UNKNOWN,
        "original_message_type": 1,
        "source_sender_id": FIRST,
        "source_chat_id": ROOM_A,
        "source_sender_label": "Earlier historical nickname",
        "source_timestamp": "0",
        "source_ids": {"svrid": "9000000000000000099"},
    }
    components = [{"kind": "reply_text", "text": "A different author's reply."}, quoted]
    observations.append(
        record(
            first,
            "unknown-parent",
            peer=ROOM_A,
            sender_id=NAMESAKE,
            text=native_components_text(components),
            message_type=49,
            appmsg_subtype=57,
            text_extraction_kind="quoted_reply",
            text_components=components,
            quoted_reference={
                key: value for key, value in quoted.items() if key not in {"kind", "text"}
            },
            server_id="9000000000000000098",
            sent_at_utc_iso=(START + timedelta(seconds=6)).isoformat(),
            sent_at_raw=str(int((START + timedelta(seconds=6)).timestamp())),
            unix_timestamp=int((START + timedelta(seconds=6)).timestamp()),
        )
    )
    merge(tmp_path, [first, second], observations, "synthetic-association-snapshot")
    history = wechat_archive_path(tmp_path)
    registry = contact_registry_path(tmp_path)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        contacts = {
            username: insert_contact(connection, username)
            for username in (FIRST, SECOND, NAMESAKE)
        }
        for metadata in (first, second):
            for username, identifier in contacts.items():
                bind(
                    connection,
                    identifier,
                    source=metadata["source_id"],
                    sender=username,
                )
    display_names_path(tmp_path).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "contacts": {
                    contacts[FIRST]: "Associated person",
                    contacts[SECOND]: "Prior separate identity",
                },
            }
        )
    )
    unknown = _identifier(first["source_id"], "unknown-parent") + "_quoted_2"
    expected[unknown] = FIRST
    fixture = SimpleNamespace(
        store=tmp_path,
        history=history,
        registry=registry,
        associations=history.with_name("contact_merges.json"),
        target=contacts[FIRST],
        member=contacts[SECOND],
        namesake=contacts[NAMESAKE],
        expected=expected,
        timestamps=timestamps,
        unknown=unknown,
        account_keys={"account:wxid_viewer_first", "account:wxid_viewer_second"},
    )
    _association(fixture)
    fixture.preserved = _preserved(fixture)
    return fixture


@pytest.fixture
def associated_app(associated_store, tmp_path):
    fixture = associated_store
    analysis.query_contact_analysis(fixture.store, fixture.target)
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
    return app, fixture


def test_old_member_link_redirects_to_canonical_person_with_view_location(associated_app):
    app, fixture = associated_app
    client = app.test_client()
    values = {
        "q": "Union",
        "sort": "newest",
        "page": "2",
        "return_to": "/browser?view=text&source=wechat&page=3#wechat_message_original",
    }
    response = client.get(
        "/browser/wechat/contacts/" + fixture.member, query_string=values
    )
    assert response.status_code in {301, 302, 307, 308}
    destination = urlsplit(response.headers["Location"])
    assert destination.path == "/browser/wechat/contacts/" + fixture.target
    assert parse_qs(destination.query) == {key: [value] for key, value in values.items()}
    result = client.get(response.headers["Location"], headers={"Accept": "application/json"})
    assert result.status_code == 200
    assert result.json["contact_id"] == fixture.target
    assert "User-confirmed person · 2 WeChat identities" in result.json["html"]
    assert fixture.target + "/export.csv" in result.json["html"]
    assert fixture.member + "/export.csv" not in result.json["html"]
    assert _preserved(fixture) == fixture.preserved


@pytest.mark.parametrize("sort", ["oldest", "newest"])
def test_associated_pagination_uses_one_order_without_changing_native_authors(associated_store, sort):
    fixture = associated_store
    rows = []
    for page in range(1, 4):
        result = analysis.query_contact_analysis(
            fixture.store, fixture.member, sort=sort, page=page, page_size=2
        )
        assert result["contact"]["id"] == fixture.target
        assert set(result["contact"]["member_contact_ids"]) == {fixture.target, fixture.member}
        assert result["total_count"] == 6
        assert result["current_page"] == page and result["total_pages"] == 3
        rows.extend(result["items"])
    assert len(rows) == len({row["id"] for row in rows}) == 6
    assert {row["id"]: row["sender_id"] for row in rows} == fixture.expected
    for row in rows:
        native_contact = fixture.target if row["sender_id"] == FIRST else fixture.member
        assert row["source_contact_id"] == native_contact
        assert row["source_contact_ids"] == [native_contact]
    known = sorted(fixture.timestamps, key=fixture.timestamps.get, reverse=sort == "newest")
    assert [row["id"] for row in rows] == [*known, fixture.unknown]
    unknown = rows[-1]
    assert unknown["timestamp_value"] == ""
    assert unknown["provenance"]["source_time"] == "0"
    repeated = [row for row in rows if row["content_text"] == REPEATED]
    assert len(repeated) == 2
    assert repeated[0]["timestamp_value"] != repeated[1]["timestamp_value"]
    assert _preserved(fixture) == fixture.preserved


def test_union_counts_and_source_links_keep_original_accounts_and_conversations(associated_app):
    app, fixture = associated_app
    result = analysis.query_contact_analysis(fixture.store, fixture.target)
    assert result["counts"] == {
        "messages": 6, "conversations": 3, "source_accounts": 2, "sources": 2
    }
    assert result["observation_count"] == 6
    assert {item["account_key"] for item in result["items"]} == fixture.account_keys
    assert {item["kind"] for item in result["items"]} == {"direct", "group"}
    client = app.test_client()
    for item in result["items"]:
        response = client.get("/browser/wechat/messages/" + item["id"])
        assert response.status_code == 302
        destination = urlsplit(response.headers["Location"])
        query = parse_qs(destination.query)
        assert query["account"] == [item["account_key"]]
        assert query["session"] == [item["conversation_id"]]
        assert destination.fragment == item["anchor_id"]
    assert _preserved(fixture) == fixture.preserved


def test_union_search_and_compact_csv_keep_both_identities_and_unknown_clock(associated_app):
    app, fixture = associated_app
    result = analysis.query_contact_analysis(fixture.store, fixture.member, query="Union")
    assert result["total_count"] == 4
    assert {item["sender_id"] for item in result["items"]} == {FIRST, SECOND}
    response = app.test_client().get(
        "/browser/wechat/contacts/" + fixture.member + "/export.csv",
        query_string={"q": "Union", "sort": "oldest"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    rows = _csv_rows(response.data)
    assert {row["Statement"] for row in rows} == {
        item["content_text"] for item in result["items"]
    }
    assert next(row for row in rows if row["Statement"] == UNKNOWN)["Time"] == ""
    assert all(
        not row["Time"] or len(row["Time"]) == 19 and row["Time"][4] == "-"
        for row in rows
    )
    repeated = app.test_client().get(
        "/browser/wechat/contacts/" + fixture.target + "/export.csv",
        query_string={"q": REPEATED, "sort": "oldest"},
    )
    copies = _csv_rows(repeated.data)
    assert len(copies) == 2
    assert copies[0]["Statement"] == copies[1]["Statement"] == REPEATED
    assert copies[0]["Time"] != copies[1]["Time"]
    assert _preserved(fixture) == fixture.preserved


def test_association_change_invalidates_union_export_and_restores_separate_views(associated_store):
    fixture = associated_store
    before = analysis.query_contact_analysis(fixture.store, fixture.target)
    _association(fixture, enabled=False)
    after = analysis.contact_analysis_scope(fixture.store, fixture.target)
    assert after["source_fingerprint"] != before["source_fingerprint"]
    with pytest.raises(analysis.ContactAnalysisSourceChanged):
        list(
            analysis.export_contact_analysis_items(
                fixture.store, fixture.target, source_fingerprint=before["source_fingerprint"]
            )
        )
    first = analysis.query_contact_analysis(fixture.store, fixture.target)
    second = analysis.query_contact_analysis(fixture.store, fixture.member)
    assert first["contact"]["id"] == fixture.target and first["total_count"] == 3
    assert second["contact"]["id"] == fixture.member and second["total_count"] == 3
    assert {item["sender_id"] for item in first["items"]} == {FIRST}
    assert {item["sender_id"] for item in second["items"]} == {SECOND}
    assert _preserved(fixture) == fixture.preserved


def test_mcp_old_member_query_reads_the_same_canonical_union(associated_store):
    fixture = associated_store
    analysis.query_contact_analysis(fixture.store, fixture.target)
    result = archive_queries.search_messages(
        fixture.store, contact_id=fixture.member, query="Union", sort="oldest", page_size=10
    )
    assert result["ok"]
    assert result["contact"]["contact_id"] == fixture.target
    assert result["contact"]["requested_contact_id"] == fixture.member
    assert set(result["contact"]["member_contact_ids"]) == {fixture.target, fixture.member}
    assert result["contact"]["association_authority"] == "user_confirmed_same_person"
    assert result["total_count"] == 4
    assert {item["sender_id"] for item in result["messages"]} == {FIRST, SECOND}
    assert all(item["scope_contact_id"] == fixture.target for item in result["messages"])
    for item in result["messages"]:
        if not item["is_embedded"]:
            native_contact = fixture.target if item["sender_id"] == FIRST else fixture.member
            assert item["sender_contact"]["contact_id"] == native_contact
    unknown = next(item for item in result["messages"] if item["text"] == UNKNOWN)
    assert unknown["timestamp_value"] == ""
    assert _preserved(fixture) == fixture.preserved
