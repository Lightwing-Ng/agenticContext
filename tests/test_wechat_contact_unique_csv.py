"""Compact contact CSV separates event identity from its two-column projection.

Code version: v1.1.0-codex.0
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_contacts import canonical_contact_id
from app.web.wechat_contact_csv import contact_csv_chunks
from app.web.wechat_csv import format_csv_time
from tests import test_wechat_contact_conversation_events as event_fixtures
from tests import test_wechat_contact_statement_groups as grouping
from tests import test_wechat_conversation_quote_events as quote_fixtures
from tests import test_wechat_forwarded_original_precedence as forwarded_fixtures
from tests.test_wechat_contact_csv import _expected_rows, _rows
from tests.test_wechat_contacts import bind


statement_store = grouping.statement_store
native_html_store = forwarded_fixtures.native_html_store
forwarded_origin_store = forwarded_fixtures.forwarded_origin_store
event_store = event_fixtures.event_store
unproven_contact_event_store = event_fixtures.unproven_contact_event_store
contact_event_store = event_fixtures.contact_event_store


def _csv(items):
    iterator = (item for item in items)
    return _rows(
        "".join(contact_csv_chunks(iterator, first=next(iterator, None))).encode(
            "utf-8"
        )
    )


def _export(fixture, cid, *, query="", sort="newest"):
    page = analysis.query_contact_analysis(
        fixture.store, cid, query=query, sort=sort, page_size=1000
    )
    items = list(
        analysis.export_contact_analysis_items(
            fixture.store,
            cid,
            query=query,
            sort=sort,
            source_fingerprint=page["source_fingerprint"],
        )
    )
    return page, items


def _source_bytes(fixture):
    return fixture.history.read_bytes(), fixture.registry.read_bytes()


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_csv_reuses_the_ipad_representative_and_the_display_order(
    contact_event_store, sort
):
    fixture = contact_event_store
    before = _source_bytes(fixture)
    page, items = _export(fixture, event_fixtures.CID, sort=sort)
    assert page["total_count"] == len(items) == 3
    assert page["observation_count"] == 6
    assert [item["id"] for item in items] == [item["id"] for item in page["items"]]
    assert {item["id"] for item in items} == {native for _html, native in fixture.pairs}
    assert all(item["source_label"] == "iPad archive" for item in items)
    assert _csv(items) == _expected_rows(page["items"])
    assert before == _source_bytes(fixture)


@pytest.mark.parametrize("unknown_time", [False, True])
def test_identical_csv_pairs_do_not_merge_their_distinct_native_events(
    statement_store, unknown_time
):
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (fixture.rows[1]["id"],)
            ).fetchone()[0]
        )
        provenance["server_id"] = "7210500216981401698"
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(provenance), fixture.rows[1]["id"]),
        )
        if unknown_time:
            connection.execute(
                "UPDATE messages SET timestamp_value='',timestamp_label='',"
                "time_basis='Unavailable source time' WHERE sender_id=?",
                (grouping.TARGET,),
            )
    before = _source_bytes(fixture)
    page, items = _export(fixture, grouping.CID)
    assert page["total_count"] == len(items) == 2
    assert len({item["statement_id"] for item in items}) == 2
    expected = _expected_rows(page["items"], unique=False)
    assert expected[0] == expected[1]
    assert _csv(items) == expected[:1]
    if unknown_time:
        assert _csv(items)[0]["Time"] == ""
    assert before == _source_bytes(fixture)


def test_same_statement_at_different_times_remains_two_export_rows(statement_store):
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (fixture.rows[1]["id"],)
            ).fetchone()[0]
        )
        provenance.update(
            server_id="7210500216981401698", unix_timestamp=grouping.EPOCH + 1
        )
        connection.execute(
            "UPDATE messages SET provenance=?,timestamp_value=?,time_sort=? WHERE id=?",
            (
                json.dumps(provenance),
                "2026-10-30T13:45:24+08:00",
                grouping.EPOCH + 1,
                fixture.rows[1]["id"],
            ),
        )
    before = _source_bytes(fixture)
    page, items = _export(fixture, grouping.CID)
    assert page["total_count"] == len(items) == 2
    rows = _csv(items)
    assert len(rows) == 2
    assert rows[0]["Statement"] == rows[1]["Statement"]
    assert rows[0]["Time"] != rows[1]["Time"]
    assert before == _source_bytes(fixture)


@pytest.mark.parametrize("offset", ["+00:00", "-05:00"])
def test_compact_clock_projection_preserves_distinct_native_events(
    statement_store, offset
):
    fixture = statement_store
    value = "2026-10-30T13:45:23" + offset
    epoch = int(datetime.fromisoformat(value).timestamp())
    with sqlite3.connect(fixture.history) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (fixture.rows[1]["id"],)
            ).fetchone()[0]
        )
        provenance.update(
            server_id="7210500216981401698",
            unix_timestamp=epoch,
        )
        connection.execute(
            "UPDATE messages SET provenance=?,timestamp_value=?,time_sort=? WHERE id=?",
            (json.dumps(provenance), value, epoch, fixture.rows[1]["id"]),
        )
    before = _source_bytes(fixture)
    page, items = _export(fixture, grouping.CID)
    assert page["total_count"] == len(items) == 2
    assert len({item["statement_id"] for item in items}) == 2
    assert {item["timestamp_value"] for item in items} == {
        "2026-10-30T13:45:23+08:00",
        value,
    }
    assert len({format_csv_time(item["timestamp_value"]) for item in items}) == 2
    rows = _csv(items)
    assert rows == [
        {"Time": "2026-10-30 13:45:23", "Statement": items[0]["content_text"]}
    ]
    assert before == _source_bytes(fixture)


def test_projection_dedup_is_exact_nonadjacent_and_preserves_first_occurrence_order():
    literal = '中文, "quoted"\r\nsecond line 😀'
    source = [
        {"timestamp_value": "2026-11-02T01:02:03+08:00", "content_text": literal},
        {"timestamp_value": "", "content_text": "Unknown clock"},
        {"timestamp_value": "2026-11-02T01:02:03+08:00", "content_text": literal},
        {"timestamp_value": "", "content_text": "Unknown clock"},
        {"timestamp_value": "2026-11-02T01:02:04+08:00", "content_text": literal},
        {"timestamp_value": "2026-11-02T01:02:03+08:00", "content_text": literal + " "},
        {
            "timestamp_value": "2026-11-02T01:02:03+08:00",
            "content_text": literal.replace("\r\n", "\n"),
        },
        {"timestamp_value": "2026-11-02T01:02:03+08:00", "content_text": "é"},
        {"timestamp_value": "2026-11-02T01:02:03+08:00", "content_text": "e\u0301"},
        {"timestamp_value": "2026-11-01T17:02:03Z", "content_text": literal},
        {"timestamp_value": "2026-11-02T01:02:03.123+08:00", "content_text": literal},
        {
            "timestamp_value": "Unrecognized source clock",
            "content_text": "Unknown clock",
        },
        {"timestamp_value": "2026-11-01T17:02:03+00:00", "content_text": literal},
        {"timestamp_value": "2026-11-02T01:02:03", "content_text": literal},
        {"timestamp_value": "2026-11-02T01:02:03+00:00", "content_text": literal},
        {"timestamp_value": "2026-11-02T01:02:03-05:00", "content_text": literal},
    ]
    before = deepcopy(source)
    closed = []

    def retained():
        try:
            yield from source
        finally:
            closed.append(True)

    iterator = retained()
    rows = _rows(
        "".join(contact_csv_chunks(iterator, first=next(iterator))).encode("utf-8")
    )
    assert rows == _expected_rows([source[index] for index in (0, 1, 4, 5, 6, 7, 8, 9)])
    assert source == before
    assert closed == [True]


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_export_spans_batches_and_all_display_pages_once(statement_store, sort):
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        originals = [
            dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (row["id"],)
                ).fetchone()
            )
            for row in fixture.rows
        ]
        for index in range(205):
            stamp = grouping.EPOCH + index + 1
            clock = datetime.fromtimestamp(
                stamp, timezone(timedelta(hours=8))
            ).isoformat()
            body = f"Unique event {index}; preserve  two spaces"
            for source, original in enumerate(originals):
                row = dict(original)
                proof = json.loads(row["provenance"])
                proof.update(server_id=str(9000000000 + index), unix_timestamp=stamp)
                row.update(
                    id=hashlib.sha256(
                        f"csv-event-{index}-{source}".encode()
                    ).hexdigest(),
                    ordinal=index + 10,
                    text=body,
                    search_text=body.casefold(),
                    timestamp_value=clock,
                    timestamp_label=clock,
                    time_sort=stamp,
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
    before = _source_bytes(fixture)
    page, items = _export(fixture, grouping.CID, sort=sort)
    assert page["total_count"] == len(items) == 206
    assert page["observation_count"] == 412
    expected = []
    first = analysis.query_contact_analysis(
        fixture.store, grouping.CID, sort=sort, page_size=17
    )
    for number in range(1, first["total_pages"] + 1):
        expected.extend(
            analysis.query_contact_analysis(
                fixture.store, grouping.CID, sort=sort, page_size=17, page=number
            )["items"]
        )
    assert [item["id"] for item in items] == [item["id"] for item in expected]
    assert _csv(items) == _expected_rows(expected)
    assert before == _source_bytes(fixture)


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_forwarded_copies_export_only_the_original_ipad_statements(
    forwarded_origin_store, sort
):
    fixture = forwarded_origin_store
    before = _source_bytes(fixture)
    page, items = _export(fixture, forwarded_fixtures.CID, sort=sort)
    assert page["total_count"] == len(items) == 6
    assert page["observation_count"] == 18
    assert {item["id"] for item in items} == set(fixture.original_ids)
    assert all(not item["is_embedded"] for item in items)
    assert _csv(items) == _expected_rows(page["items"])
    assert before == _source_bytes(fixture)


def test_current_quoted_reply_is_exported_once_without_the_quoted_authors_words(
    event_store,
):
    fixture = event_store
    _html, native = quote_fixtures._add_quote(fixture)
    with sqlite3.connect(fixture.registry) as connection:
        for sender in (quote_fixtures.fixtures.OWNER, quote_fixtures.fixtures.PEER):
            cid = canonical_contact_id(sender, authority="native_contact_username")
            bind(connection, cid, source=quote_fixtures.fixtures.NATIVE, sender=sender)
    connection, proof = quote_fixtures.fixtures._run(fixture)
    connection.close()
    assert proof["count_delta"] == 4
    cid = canonical_contact_id(
        quote_fixtures.fixtures.OWNER, authority="native_contact_username"
    )
    page = analysis.query_contact_analysis(fixture.store, cid)
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    items = list(
        analysis.export_contact_analysis_items(
            fixture.store, cid, source_fingerprint=page["source_fingerprint"]
        )
    )
    assert len(items) == page["total_count"] == 2
    current = next(item for item in items if item["id"] == native)
    assert current["content_text"] == quote_fixtures.REPLY
    rows = _csv(items)
    assert sum(row["Statement"] == quote_fixtures.REPLY for row in rows) == 1
    assert not any(quote_fixtures.QUOTED in row["Statement"] for row in rows)
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )
