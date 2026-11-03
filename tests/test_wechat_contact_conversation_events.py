"""Contact statements reuse proven conversation events without losing sources.

Code version: v1.0.0-codex.0
"""

from collections import Counter
from datetime import datetime
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_conversation_events as events
from app.core.wechat_archive import _identifier
from app.core.wechat_contacts import canonical_contact_id
from app.core.wechat_text import native_components_text
from tests import test_wechat_conversation_events as event_fixtures
from tests.test_wechat_contacts import bind


event_store = event_fixtures.event_store
CID = canonical_contact_id(event_fixtures.PEER, authority="native_contact_username")


@pytest.fixture
def contact_event_store(event_store):
    fixture = event_store
    history = fixture.repository.path
    wall = "2020-01-01T08:00:03"
    body = "Private event body 3; preserve  two spaces"
    html_id = hashlib.sha256(b"html-3").hexdigest()
    native_id = _identifier(event_fixtures.NATIVE, "native-3")
    with sqlite3.connect(history) as connection:
        connection.row_factory = sqlite3.Row
        for original, identifier, ordinal, native in (
            (fixture.ids[-1][0], html_id, 4, False),
            (fixture.ids[-1][1], native_id, 104, True),
        ):
            row = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (original,)
                ).fetchone()
            )
            proof = json.loads(row["provenance"])
            if native:
                proof.update(
                    record_id="native-3",
                    server_id="922337203685477583",
                    unix_timestamp=int(
                        datetime.fromisoformat(wall + "+08:00").timestamp()
                    ),
                )
                proof["locator"]["local_id"] = 103
            else:
                proof.update(
                    record_id="html-record-3",
                    source_order=4,
                    source_time=wall.replace("T", " "),
                    primary_text_sha256=hashlib.sha256(body.encode()).hexdigest(),
                )
                # Export-local numbering may reset; it is not an event identifier.
                proof["locator"]["export_msgid"] = "1"
            row.update(
                id=identifier,
                ordinal=ordinal,
                text=body,
                search_text=body.casefold(),
                time_sort=4,
                timestamp_value=wall + "+08:00" if native else wall,
                timestamp_label="1 Jan 2020 08:00:03",
                provenance=json.dumps(proof),
            )
            connection.execute(
                "INSERT INTO messages("
                + ",".join(row)
                + ") VALUES("
                + ",".join("?" for _ in row)
                + ")",
                tuple(row.values()),
            )
        connection.execute(
            "UPDATE conversations SET message_count=8,last_sort=4,last_time=? WHERE id=?",
            (wall + "+08:00", event_fixtures.SESSION),
        )
    with sqlite3.connect(fixture.registry) as connection:
        for sender in (event_fixtures.OWNER, event_fixtures.PEER):
            contact = connection.execute(
                "SELECT id FROM contacts WHERE username=?", (sender,)
            ).fetchone()[0]
            bind(connection, contact, source=event_fixtures.NATIVE, sender=sender)
    pairs = [fixture.ids[0], fixture.ids[2], (html_id, native_id)]
    return SimpleNamespace(
        store=fixture.store,
        history=history,
        registry=fixture.registry,
        repository=fixture.repository,
        pairs=pairs,
    )


def _sources(fixture):
    return fixture.history.read_bytes(), fixture.registry.read_bytes()


def _no_scan(*_args, **_kwargs):
    raise AssertionError("Complete contact references must not rescan source authors.")


def _legacy_group_cache(fixture):
    analysis.query_contact_analysis(fixture.store, CID)
    old_version = "v1.7.1-codex.0"
    identities = {
        str(path.resolve()): analysis._identity(path)
        for path in (fixture.history, fixture.registry)
    }
    fingerprint = hashlib.sha256(
        analysis._json([old_version, identities]).encode()
    ).hexdigest()
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    with sqlite3.connect(cache) as connection:
        rows = connection.execute("SELECT id FROM refs ORDER BY id").fetchall()
        connection.execute("DELETE FROM statement_members")
        connection.execute("DELETE FROM statement_groups")
        for (reference,) in rows:
            statement = hashlib.sha256(
                ("legacy-independent-statement:" + reference).encode()
            ).hexdigest()
            connection.execute(
                "INSERT INTO statement_groups VALUES(?,?,?)",
                (statement, reference, "retained-receiving-conversation"),
            )
            connection.execute(
                "INSERT INTO statement_members VALUES(?,?)",
                (reference, statement),
            )
        connection.executemany(
            "UPDATE info SET value=? WHERE key=?",
            [(old_version, "version"), (fingerprint, "fingerprint")],
        )
    return cache


def test_contact_statements_prefer_ipad_and_keep_every_source_reference(
    contact_event_store,
):
    fixture = contact_event_store
    before = _sources(fixture)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["status"] == "ready"
    assert page["total_count"] == page["counts"]["messages"] == 3
    assert page["observation_count"] == 6
    assert {item["id"] for item in page["items"]} == {
        native for _html, native in fixture.pairs
    }
    expected = {native: {html, native} for html, native in fixture.pairs}
    for item in page["items"]:
        assert item["source_id"] == event_fixtures.NATIVE
        assert item["source_label"] == "iPad archive"
        assert item["timestamp_value"].endswith("+08:00")
        assert item["observation_count"] == 2
        assert {source["id"] for source in item["observations"]} == expected[item["id"]]
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    with sqlite3.connect(cache) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 6
        assert (
            connection.execute("SELECT COUNT(*) FROM statement_members").fetchone()[0]
            == 6
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM statement_groups").fetchone()[0]
            == 3
        )
        dump = "\n".join(connection.iterdump())
        assert "Private event body" not in dump
    assert before == _sources(fixture)


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_contact_event_pagination_and_search_use_preferred_groups(
    contact_event_store,
    sort,
):
    fixture = contact_event_store
    before = _sources(fixture)
    page = analysis.query_contact_analysis(
        fixture.store, CID, sort=sort, page_size=2, page=2
    )
    assert page["total_count"] == 3
    assert page["total_pages"] == page["current_page"] == 2
    assert len(page["items"]) == 1
    found = analysis.query_contact_analysis(
        fixture.store, CID, query="Private event body 2", sort=sort
    )
    assert found["total_count"] == 1
    assert found["observation_count"] == 2
    assert found["items"][0]["id"] == fixture.pairs[1][1]
    assert before == _sources(fixture)


def test_contact_csv_keeps_both_source_occurrences_of_each_event(contact_event_store):
    fixture = contact_event_store
    before = _sources(fixture)
    page = analysis.query_contact_analysis(fixture.store, CID)
    rows = list(
        analysis.export_contact_analysis_items(
            fixture.store, CID, source_fingerprint=page["source_fingerprint"]
        )
    )
    assert len(rows) == 6
    assert {row["id"] for row in rows} == {
        identifier for pair in fixture.pairs for identifier in pair
    }
    assert sorted(Counter(row["statement_id"] for row in rows).values()) == [2, 2, 2]
    assert before == _sources(fixture)


def test_complete_legacy_group_upgrade_reuses_all_retained_references(
    contact_event_store,
    monkeypatch,
):
    fixture = contact_event_store
    cache = _legacy_group_cache(fixture)
    before = _sources(fixture)
    for name in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
    ):
        monkeypatch.setattr(analysis, name, _no_scan)
    monkeypatch.setattr(events, "_scan", _no_scan)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 3
    assert page["observation_count"] == 6
    with sqlite3.connect(cache) as connection:
        assert (
            dict(connection.execute("SELECT key,value FROM info"))["version"]
            == analysis.INDEX_VERSION
        )
        assert connection.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 6
    assert before == _sources(fixture)


def test_warm_group_query_does_not_revisit_conversation_or_author_scans(
    contact_event_store,
    monkeypatch,
):
    fixture = contact_event_store
    first = analysis.query_contact_analysis(fixture.store, CID)
    before = _sources(fixture)
    for name in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
    ):
        monkeypatch.setattr(analysis, name, _no_scan)
    monkeypatch.setattr(events, "conversation_events", _no_scan)
    page = analysis.query_contact_analysis(fixture.store, CID, query="event body")
    assert page["source_fingerprint"] == first["source_fingerprint"]
    assert page["total_count"] == 3
    assert page["observation_count"] == 6
    assert before == _sources(fixture)


def test_complete_upgrade_preserves_existing_embedded_original_membership(
    contact_event_store,
    monkeypatch,
):
    fixture = contact_event_store
    original = fixture.pairs[1][1]
    parent = _identifier(event_fixtures.NATIVE, "native-quoted-receipt")
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (original,)
            ).fetchone()
        )
        provenance = json.loads(row["provenance"])
        quoted = {
            "kind": "quoted_text",
            "text": row["text"],
            "original_message_type": 1,
            "source_sender_id": event_fixtures.PEER,
            "source_sender_label": "Original source label",
            "source_timestamp": "0",
            "source_chat_id": "",
            "source_ids": {"svrid": str(provenance["server_id"])},
        }
        components = [{"kind": "reply_text", "text": "Owner's reply"}, quoted]
        provenance.update(
            record_id="native-quoted-receipt",
            message_type=49,
            appmsg_subtype=57,
            text_extraction_kind="quoted_reply",
            text_components=components,
            quoted_reference={
                key: value
                for key, value in quoted.items()
                if key not in {"kind", "text"}
            },
            server_id="922337203685477587",
            unix_timestamp=int(
                datetime.fromisoformat("2020-01-01T08:00:04+08:00").timestamp()
            ),
        )
        provenance["locator"]["local_id"] = 104
        body = native_components_text(components)
        row.update(
            id=parent,
            ordinal=105,
            sender_id=event_fixtures.OWNER,
            sender_label="Receipt owner",
            direction="sent",
            text=body,
            search_text=body.casefold(),
            time_sort=5,
            timestamp_value="2020-01-01T08:00:04+08:00",
            timestamp_label="1 Jan 2020 08:00:04",
            provenance=json.dumps(provenance),
        )
        connection.execute(
            "INSERT INTO messages("
            + ",".join(row)
            + ") VALUES("
            + ",".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
        connection.execute(
            "UPDATE conversations SET message_count=message_count+1 WHERE id=?",
            (event_fixtures.SESSION,),
        )
    analysis.query_contact_analysis(fixture.store, CID)
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    reference = parent + "_quoted_2"
    with sqlite3.connect(cache) as connection:
        linked = dict(
            connection.execute(
                "SELECT reference_id,statement_id FROM statement_members "
                "WHERE reference_id IN (?,?)",
                (original, reference),
            )
        )
        assert linked[original] == linked[reference]
        old_version = "v1.7.1-codex.0"
        identities = {
            str(path.resolve()): analysis._identity(path)
            for path in (fixture.history, fixture.registry)
        }
        fingerprint = hashlib.sha256(
            analysis._json([old_version, identities]).encode()
        ).hexdigest()
        connection.executemany(
            "UPDATE info SET value=? WHERE key=?",
            [(old_version, "version"), (fingerprint, "fingerprint")],
        )
    before = _sources(fixture)
    for name in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
        "_group_statements",
    ):
        monkeypatch.setattr(analysis, name, _no_scan)
    monkeypatch.setattr(events, "_scan", _no_scan)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 3
    assert page["observation_count"] == 7
    with sqlite3.connect(cache) as connection:
        retained = dict(
            connection.execute(
                "SELECT reference_id,statement_id FROM statement_members "
                "WHERE reference_id IN (?,?)",
                (original, reference),
            )
        )
        assert retained == linked
        assert (
            connection.execute(
                "SELECT component_path FROM refs WHERE id=?", (reference,)
            ).fetchone()[0]
            != "[]"
        )
    assert before == _sources(fixture)


@pytest.mark.parametrize(
    "conflict,groups,observations",
    [
        ("html_body", 4, 6),
        ("different_native_event", 5, 7),
    ],
)
def test_unproven_or_conflicting_sources_are_not_discarded(
    contact_event_store,
    conflict,
    groups,
    observations,
):
    fixture = contact_event_store
    html_id, native_id = fixture.pairs[0]
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        if conflict == "html_body":
            changed = "An independently retained HTML conflict"
            proof = json.loads(
                connection.execute(
                    "SELECT provenance FROM messages WHERE id=?", (html_id,)
                ).fetchone()[0]
            )
            proof["primary_text_sha256"] = hashlib.sha256(changed.encode()).hexdigest()
            connection.execute(
                "UPDATE messages SET text=?,search_text=?,provenance=? WHERE id=?",
                (changed, changed.casefold(), json.dumps(proof), html_id),
            )
        else:
            copied = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (native_id,)
                ).fetchone()
            )
            proof = json.loads(copied["provenance"])
            proof.update(
                record_id="native-distinct-event", server_id="922337203685477589"
            )
            proof["locator"]["local_id"] = 104
            copied.update(
                id=_identifier(event_fixtures.NATIVE, proof["record_id"]),
                ordinal=105,
                provenance=json.dumps(proof),
            )
            connection.execute(
                "INSERT INTO messages("
                + ",".join(copied)
                + ") VALUES("
                + ",".join("?" for _ in copied)
                + ")",
                tuple(copied.values()),
            )
            connection.execute(
                "UPDATE conversations SET message_count=message_count+1 WHERE id=?",
                (event_fixtures.SESSION,),
            )
    before = _sources(fixture)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == groups
    assert page["observation_count"] == observations
    represented = {
        source["id"] for item in page["items"] for source in item["observations"]
    }
    assert {html_id, native_id} <= represented
    assert before == _sources(fixture)
