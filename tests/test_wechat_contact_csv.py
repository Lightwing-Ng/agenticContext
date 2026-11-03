"""Contact CSV exposes canonical export time and original statement text.

Code version: v2.1.1-codex.0
"""

from __future__ import annotations

import csv
from datetime import datetime
import io
import json
import sqlite3
from urllib.parse import quote, urlencode
from urllib.request import urlopen

import pytest
from playwright.sync_api import expect

from app.core.wechat_contact_analysis import (
    ContactAnalysisSourceChanged,
    export_contact_analysis_items,
    query_contact_analysis,
)
from tests import test_wechat_contact_analysis_e2e as browser_fixtures
from tests import test_wechat_contact_analysis_routes as fixtures
from app.web.wechat_contact_csv import contact_csv_chunks
from tests import test_wechat_contact_statement_groups as statement_groups
from tests import test_wechat_forwarded_contact_analysis as forwarded


contact_app = fixtures.contact_app
contact_analysis_store = fixtures.contact_analysis_store
forwarded_contact_app = fixtures.forwarded_contact_app
forwarded_activity_store = fixtures.forwarded_activity_store
contact_analysis_server = browser_fixtures.contact_analysis_server
disposable_browser = browser_fixtures.disposable_browser
CID = fixtures.CID
statement_store = statement_groups.statement_store


def _rows(body):
    assert body.startswith(b"\xef\xbb\xbf")
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline=""))
    assert reader.fieldnames == ["Time", "Statement"]
    return list(reader)


def _expected_time(value):
    if not value:
        return ""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    clock = parsed.strftime("%Y-%m-%d %H:%M:%S")
    offset = parsed.strftime("%z")
    return clock + (f" UTC{offset[:3]}:{offset[3:]}" if offset else "")


def _expected_rows(items):
    return [
        {
            "Time": _expected_time(item.get("timestamp_value")),
            "Statement": item["content_text"],
        }
        for item in items
    ]


def test_csv_round_trips_unicode_multiline_text_with_only_requested_columns(
    contact_app,
):
    app, fixture = contact_app
    text = '中文, "quoted"\r\nsecond line 😀'
    with sqlite3.connect(fixture.history) as connection:
        identifier = connection.execute(
            "SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "WHERE c.partition_id LIKE 'ios-%' AND m.sender_id=? ORDER BY m.id LIMIT 1",
            (fixtures.fixtures.TARGET,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE messages SET text=?,search_text=? WHERE id=?",
            (text, text.casefold(), identifier),
        )
    expected = query_contact_analysis(fixture.store, CID, sort="oldest")
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    response = app.test_client().get(
        f"/browser/wechat/contacts/{CID}/export.csv?sort=oldest&page=99"
    )
    assert response.status_code == 200
    assert response.is_streamed
    assert response.mimetype == "text/csv"
    assert response.cache_control.no_store
    assert "attachment;" in response.headers["Content-Disposition"]
    assert "filename*=UTF-8''" in response.headers["Content-Disposition"]
    rows = _rows(response.data)
    assert rows == _expected_rows(expected["items"])
    assert any(row["Statement"] == text for row in rows)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_csv_exports_more_than_one_page_in_the_display_order(
    contact_analysis_server, sort
):
    fixture = contact_analysis_server
    before = fixture.history.stat(), fixture.registry.stat()
    expected = []
    for page in range(1, 6):
        expected.extend(
            query_contact_analysis(
                fixture.store, fixture.target_id, sort=sort, page=page
            )["items"]
        )
    with urlopen(
        f"{fixture.url}/browser/wechat/contacts/{fixture.target_id}/export.csv?sort={sort}&page=2"
    ) as response:
        rows = _rows(response.read())
    assert len(rows) == 215
    assert rows == _expected_rows(expected)
    assert {item["account_key"] for item in expected} == {
        "account:wxid_account_a",
        "account:wxid_account_b",
    }
    assert not any("OTHER FLORA" in row["Statement"] for row in rows)
    after = fixture.history.stat(), fixture.registry.stat()
    assert [(value.st_size, value.st_mtime_ns) for value in before] == [
        (value.st_size, value.st_mtime_ns) for value in after
    ]


def test_search_and_empty_csv_match_the_contact_view(contact_app):
    app, fixture = contact_app
    sample = query_contact_analysis(fixture.store, CID)["items"][0]["content_text"]
    for query in (sample[:10], "no-such-statement-unique"):
        expected = query_contact_analysis(
            fixture.store, CID, query=query, sort="oldest"
        )
        response = app.test_client().get(
            f"/browser/wechat/contacts/{CID}/export.csv",
            query_string={"q": query, "sort": "oldest"},
        )
        assert response.status_code == 200
        rows = _rows(response.data)
        assert rows == _expected_rows(expected["items"])
        assert len(rows) == expected["total_count"]


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_forwarded_csv_preserves_display_time_and_own_text(forwarded_contact_app, sort):
    app, fixture = forwarded_contact_app
    expected = query_contact_analysis(fixture["store"], forwarded.CID, sort=sort)
    response = app.test_client().get(
        f"/browser/wechat/contacts/{forwarded.CID}/export.csv?sort={sort}"
    )
    rows = _rows(response.data)
    assert len(rows) == len(expected["items"])
    for row, item in zip(rows, expected["items"], strict=True):
        assert row == _expected_rows([item])[0]
        assert forwarded.QUOTED not in row["Statement"]
        assert forwarded.DESCENDANT not in row["Statement"]


def test_export_rejects_invalid_contact_and_busy_source(contact_app):
    app, fixture = contact_app
    client = app.test_client()
    for identifier in ("invalid", "wechat:contact:" + "0" * 64):
        assert (
            client.get(
                f"/browser/wechat/contacts/{quote(identifier)}/export.csv"
            ).status_code
            == 404
        )
    fixture.history.with_name(fixture.history.name + "-journal").write_bytes(b"busy")
    response = client.get(f"/browser/wechat/contacts/{CID}/export.csv")
    assert response.status_code == 503
    assert "Content-Disposition" not in response.headers


def test_iterator_stops_before_next_batch_after_source_changes(contact_analysis_server):
    fixture = contact_analysis_server
    prepared = query_contact_analysis(fixture.store, fixture.target_id)
    items = export_contact_analysis_items(
        fixture.store,
        fixture.target_id,
        source_fingerprint=prepared["source_fingerprint"],
    )
    journal = fixture.history.with_name(fixture.history.name + "-journal")
    try:
        assert len([next(items) for _ in range(200)]) == 200
        journal.write_bytes(b"new source publication")
        with pytest.raises(ContactAnalysisSourceChanged):
            next(items)
    finally:
        items.close()
        journal.unlink(missing_ok=True)


@pytest.mark.parametrize("width", [390, 855, 1280])
def test_contact_csv_button_downloads_all_search_results_without_navigation(
    disposable_browser, contact_analysis_server, tmp_path, width
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000}, accept_downloads=True
    )
    try:
        page = context.new_page()
        query = urlencode({"q": "A target", "sort": "oldest", "page": 2})
        url = f"{fixture.url}/browser/wechat/contacts/{fixture.target_id}?{query}"
        page.goto(url)
        button = page.get_by_role("link", name="Export CSV", exact=True)
        expect(button).to_be_visible()
        assert button.get_attribute("class") == "secondary-button"
        geometry = button.evaluate("""button => {
            const box = button.getBoundingClientRect();
            const search = document.querySelector('.wechat-contact-search').getBoundingClientRect();
            return {left: box.left, right: box.right,
                overlap: box.left < search.right && box.right > search.left
                    && box.top < search.bottom && box.bottom > search.top,
                overflow: document.documentElement.scrollWidth > innerWidth};
        }""")
        assert 0 <= geometry["left"] < geometry["right"] <= width
        assert not geometry["overlap"] and not geometry["overflow"]
        with page.expect_download() as pending:
            button.click()
        download = pending.value
        assert download.failure() is None
        assert download.suggested_filename.endswith(".csv")
        rows = _rows(download.path().read_bytes())
        assert len(rows) == 107
        expected = query_contact_analysis(
            fixture.store,
            fixture.target_id,
            query="A target",
            sort="oldest",
            page_size=1000,
        )
        assert rows == _expected_rows(expected["items"])
        assert page.url == url
        expect(button).to_be_visible()
        page.screenshot(path=str(tmp_path / f"contact-csv-{width}.png"))
    finally:
        context.close()


@pytest.mark.parametrize(
    "source_items",
    [
        [],
        [
            {
                "timestamp_value": "",
                "timestamp_label": "Unknown time",
                "content_text": "Uncertain clock, original statement",
            }
        ],
    ],
)
def test_csv_keeps_unknown_time_blank_and_exact_headers_for_empty_results(source_items):
    closed = []

    def source():
        try:
            yield from source_items
        finally:
            closed.append(True)

    items = source()
    first = next(items, None)
    body = "".join(contact_csv_chunks(items, first=first)).encode("utf-8")
    assert _rows(body) == _expected_rows(source_items)
    assert closed == [True]


def test_two_column_csv_uses_preferred_statements_and_keeps_source_inspection(statement_store):
    fixture = statement_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = query_contact_analysis(fixture.store, statement_groups.CID)
    assert page["total_count"] == 1
    items = export_contact_analysis_items(
        fixture.store,
        statement_groups.CID,
        source_fingerprint=page["source_fingerprint"],
    )
    first = next(items, None)
    body = "".join(contact_csv_chunks(items, first=first)).encode("utf-8")
    rows = _rows(body)
    assert rows == _expected_rows(page["items"])
    sources = list(export_contact_analysis_items(
        fixture.store,
        statement_groups.CID,
        source_fingerprint=page["source_fingerprint"],
        source_occurrences=True,
    ))
    assert len(sources) == page["observation_count"] == 2
    assert {item["id"] for item in sources} == {
        item["id"] for item in page["items"][0]["observations"]
    }
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_identical_csv_values_preserve_distinct_native_events(statement_store, sort):
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        proof = json.loads(connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (fixture.rows[1]["id"],)
        ).fetchone()[0])
        proof["server_id"] = "7000000000000000002"
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(proof), fixture.rows[1]["id"]),
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = query_contact_analysis(fixture.store, statement_groups.CID, sort=sort)
    assert page["total_count"] == page["observation_count"] == 2
    items = export_contact_analysis_items(
        fixture.store, statement_groups.CID, sort=sort,
        source_fingerprint=page["source_fingerprint"],
    )
    first = next(items)
    rows = _rows("".join(contact_csv_chunks(items, first=first)).encode("utf-8"))
    assert len(rows) == 2
    assert rows[0] == rows[1]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
