"""Contact aggregation defaults to chronological reading with matching row numbers.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from html.parser import HTMLParser
import io
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.config import CrawlConfig
from app.core.wechat_archive import _identifier
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from app.web.local_resource_routes import wechat_contact_analysis_url
from app.web.wechat_csv import format_csv_time
from tests import test_wechat_contact_analysis as fixtures


CID = fixtures.CID
contact_analysis_store = fixtures.contact_analysis_store


class StatementRows(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "tr" and "data-contact-message-id" in attributes:
            self.row = {"id": attributes["data-contact-message-id"], "cells": []}
        elif tag == "td" and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.row is not None:
            self.row["cells"].append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


@pytest.fixture
def chronological_store(contact_analysis_store):
    fixture = contact_analysis_store
    added = []
    start = datetime.fromisoformat("2020-01-01T10:00:00+08:00")
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        template = dict(
            connection.execute(
                "SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                "WHERE c.partition_id LIKE 'ios-%' AND m.sender_id=? ORDER BY m.id LIMIT 1",
                (fixtures.TARGET,),
            ).fetchone()
        )
        source = connection.execute(
            "SELECT partition_id FROM conversations WHERE id=?",
            (template["conversation_id"],),
        ).fetchone()[0]
        for number in range(107):
            unknown = number >= 105
            stamp = start + timedelta(minutes=number)
            record = "chronology-sequence-" + str(number)
            text = "Chronology sequence " + (
                "unknown " + str(number)
                if unknown
                else ("even " if number % 2 == 0 else "odd ") + str(number)
            )
            proof = json.loads(template["provenance"])
            proof.update(
                record_id=record,
                server_id=str(8100000000000000000 + number),
                unix_timestamp=0 if unknown else int(stamp.timestamp()),
                source_time="" if unknown else stamp.isoformat(),
            )
            proof["locator"] = {**proof.get("locator", {}), "local_id": 2000 + number}
            row = {
                **template,
                "id": _identifier(source, record),
                "ordinal": 2000 + number,
                "text": text,
                "search_text": text.casefold(),
                "timestamp_value": "" if unknown else stamp.isoformat(),
                "timestamp_label": "Unknown time"
                if unknown
                else stamp.strftime("%-d %b %Y %H:%M:%S"),
                "time_sort": 0 if unknown else stamp.timestamp(),
                "provenance": json.dumps(proof),
            }
            connection.execute(
                "INSERT INTO messages("
                + ",".join(row)
                + ") VALUES("
                + ",".join("?" for _ in row)
                + ")",
                tuple(row.values()),
            )
            added.append(row["id"])
    initial = analysis.query_contact_analysis(
        fixture.store, CID, sort="oldest", page_size=200
    )
    assert initial["status"] == "ready" and initial["total_count"] == 116
    assert [item["id"] for item in initial["items"][:105]] == added[:105]
    assert initial["items"][-1]["id"] == added[-1]
    return SimpleNamespace(
        **vars(fixture),
        added=added,
        before=(fixture.history.read_bytes(), fixture.registry.read_bytes()),
    )


@pytest.fixture
def chronological_app(chronological_store, tmp_path):
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            chronological_store.store,
            computer_use_settings_path=tmp_path / "agent.json",
            computer_use_runtime_root=tmp_path / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    return app, chronological_store


def _unchanged(fixture):
    assert fixture.before == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    )


def _fragment(app, **query):
    response = app.test_client().get(
        "/browser/wechat/contacts/" + CID,
        query_string=query,
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200 and response.json["status"] == "ready"
    return response.json["html"]


@pytest.mark.parametrize("sort", [None, "", "invalid"])
def test_core_defaults_and_invalid_sorts_read_oldest_first(chronological_store, sort):
    fixture = chronological_store
    kwargs = {} if sort is None else {"sort": sort}
    page = analysis.query_contact_analysis(fixture.store, CID, page_size=200, **kwargs)
    assert [item["id"] for item in page["items"][:105]] == fixture.added[:105]
    assert page["items"][-1]["id"] == fixture.added[-1]
    assert page["items"][-1]["timestamp_value"] == ""
    assert page["items"][-1]["timestamp_label"] == "Unknown time"
    assert [item["sequence_number"] for item in page["items"]] == list(range(1, 117))
    _unchanged(fixture)


@pytest.mark.parametrize("sort", [None, "invalid", "oldest", "newest"])
def test_contact_route_pages_use_chronological_numbering(chronological_app, sort):
    app, fixture = chronological_app
    query = {} if sort is None else {"sort": sort}
    rows = []
    for page in (1, 2, 3):
        fragment = _fragment(app, **query, page=page)
        parsed = StatementRows(fragment).rows
        assert len(parsed) == (50 if page < 3 else 16)
        rows.extend(parsed)
    newest = sort == "newest"
    expected_numbers = (
        list(range(114, 0, -1)) + [116, 115] if newest else list(range(1, 117))
    )
    assert [int(row["cells"][0]) for row in rows] == expected_numbers
    selected = [row["id"] for row in rows if row["id"] in fixture.added[:105]]
    assert selected == (
        list(reversed(fixture.added[:105])) if newest else fixture.added[:105]
    )
    unknown = list(reversed(fixture.added[-2:])) if newest else fixture.added[-2:]
    assert [row["id"] for row in rows[-2:]] == unknown
    assert all(row["cells"][1] == "Unknown time" for row in rows[-2:])
    _unchanged(fixture)


@pytest.mark.parametrize("sort", ["oldest", "newest"])
def test_search_scopes_numbers_and_pagination_to_matching_statements(
    chronological_app, sort
):
    app, fixture = chronological_app
    rows = [
        row
        for page in (1, 2)
        for row in StatementRows(
            _fragment(app, q="Chronology sequence even", sort=sort, page=page)
        ).rows
    ]
    expected = fixture.added[:105:2]
    if sort == "newest":
        expected = list(reversed(expected))
    assert [row["id"] for row in rows] == expected
    assert [int(row["cells"][0]) for row in rows] == list(
        range(53, 0, -1) if sort == "newest" else range(1, 54)
    )
    _unchanged(fixture)


def test_new_contact_entry_uses_oldest_without_changing_return_conversation(
    chronological_app,
):
    app, fixture = chronological_app
    origin = (
        "/browser?view=text&source=wechat&sort=newest&page=12&session=wechat%3Aroom"
    )
    with app.test_request_context(origin):
        destination = wechat_contact_analysis_url(
            {"id": CID, "platform": "wechat", "identity_status": "verified"},
            "a" * 64,
        )
    query = parse_qs(urlsplit(destination).query)
    assert query["sort"] == ["oldest"]
    assert query["return_to"] == [origin + "#wechat_message_" + "a" * 64]
    assert parse_qs(urlsplit(query["return_to"][0]).query)["sort"] == ["newest"]
    _unchanged(fixture)


@pytest.mark.parametrize("sort", [None, "invalid", "oldest", "newest"])
def test_csv_and_core_export_keep_default_or_explicit_display_order(
    chronological_app, sort
):
    app, fixture = chronological_app
    query = {} if sort is None else {"sort": sort}
    response = app.test_client().get(
        "/browser/wechat/contacts/" + CID + "/export.csv",
        query_string=query,
    )
    assert response.status_code == 200 and response.is_streamed
    rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig"))))
    assert len(rows) == 113
    newest = sort == "newest"
    sequence = [
        row["Statement"]
        for row in rows
        if row["Statement"].startswith("Chronology sequence")
    ]
    expected = [
        "Chronology sequence " + ("even " if number % 2 == 0 else "odd ") + str(number)
        for number in (range(104, -1, -1) if newest else range(105))
    ]
    unknown = [
        "Chronology sequence unknown " + str(number)
        for number in ((106, 105) if newest else (105, 106))
    ]
    assert sequence == expected + unknown
    assert rows[-2:] == [{"Time": "", "Statement": text} for text in unknown]
    context = analysis.contact_analysis_export_context(fixture.store, CID)
    exported = list(
        analysis.export_contact_analysis_items(
            fixture.store,
            CID,
            source_fingerprint=context["source_fingerprint"],
            **query,
        )
    )
    assert len(exported) == 116
    unique = dict.fromkeys(
        (format_csv_time(item.get("timestamp_value"))[:19], item["content_text"])
        for item in exported
    )
    assert rows == [{"Time": clock, "Statement": text} for clock, text in unique]
    _unchanged(fixture)
