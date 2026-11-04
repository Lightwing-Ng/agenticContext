"""Session exports retain source parents and expose proved child source clocks.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import csv
from dataclasses import replace
import io
import json
import sqlite3
from unittest.mock import patch

import pytest

from app.core.chat_history_browser import build_chat_history_markdown, query_chat_history
from app.core.config import CrawlConfig
from app.core.wechat_forwarded import apply_forwarded_clock_evidence
from app.core.wechat_forwarded_clocks import WechatForwardedClocks
from app.core.wechat_session_export import (
    WechatSessionExport,
    WechatSessionSourceChanged,
    forwarded_export_items,
)
from app.web.wechat_session_csv import session_csv_chunks
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_wechat_forwarded_clock_integration as integration
from tests import test_wechat_forwarded_clocks as fixtures


native_html_store = fixtures.native_html_store
statement_store = fixtures.statement_store
forwarded_origin_store = fixtures.forwarded_origin_store
pinned_clock_store = fixtures.pinned_clock_store


def _session(fixture):
    with sqlite3.connect(fixture.history) as connection:
        return connection.execute(
            "SELECT conversation_id FROM messages WHERE id=?", (fixture.html_parent,)
        ).fetchone()[0]


def _csv(fixture):
    export = WechatSessionExport(fixture.store, _session(fixture))
    return list(csv.DictReader(io.StringIO("".join(session_csv_chunks(export)).lstrip("\ufeff"))))


def test_csv_retains_parent_and_exports_every_proved_packet_child(pinned_clock_store):
    fixture = pinned_clock_store
    parent = fixtures._rows(fixture)[0]
    children = json.loads(parent["provenance"])["forwarded_components"]
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    rows = _csv(fixture)
    assert list(rows[0]) == ["Time", "Speaker ID", "Statement"]
    position = next(index for index, row in enumerate(rows) if row["Statement"] == parent["text"])
    assert rows[position]["Time"] == parent["timestamp_value"].replace("T", " ")
    forwarded = rows[position + 1 : position + 1 + len(children)]
    assert [row["Statement"] for row in forwarded] == [child["text"] for child in children]
    assert forwarded[0]["Time"] == "2020-08-12 13:46:00"
    assert children[0]["sender_native_id"] in forwarded[0]["Speaker ID"]
    assert all(row["Time"] and not row["Time"].startswith("1970") for row in forwarded)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_known_child_clock_is_included_beside_recovered_packet_children(pinned_clock_store):
    fixture = pinned_clock_store
    fixtures._mutate(
        fixture,
        native=False,
        callback=lambda proof: proof["forwarded_components"][0].update(
            raw_timestamp="2020-08-12 13:46:00"
        ),
    )
    fixtures._pin(fixture)
    rows = _csv(fixture)
    children = json.loads(fixtures._rows(fixture)[0]["provenance"])["forwarded_components"]
    for child in children:
        assert any(row["Statement"] == child["text"] and row["Time"] for row in rows)


def test_markdown_retains_source_and_discloses_native_child_clock_proof(pinned_clock_store):
    fixture = pinned_clock_store
    parent = fixtures._rows(fixture)[0]
    page = query_chat_history(
        fixture.store, source="wechat", session=_session(fixture), session_view=True
    )
    markdown = build_chat_history_markdown(page)
    assert parent["text"] in markdown
    assert "#### Forwarded statements" in markdown
    assert "12 Aug 2020 13:46:00" in markdown
    assert '- Original source clock: "1970-01-01 08:00:00"' in markdown
    assert fixture.native_parent in markdown
    assert "Recovered native packet time; Source clock; timezone unknown" in markdown


def test_unknown_child_never_inherits_parent_or_sibling_clock(pinned_clock_store):
    fixture = pinned_clock_store
    record = integration._record(fixture)
    clocks = WechatForwardedClocks(fixture.store).project(fixtures._rows(fixture))[fixture.html_parent]
    clocks.pop((2,))
    apply_forwarded_clock_evidence(record, clocks)
    items = forwarded_export_items(record)
    assert len(items) == 9
    assert items[0]["timestamp_value"]
    assert items[1]["timestamp_value"] == ""
    assert items[1]["time_basis"] == "Unavailable source time"
    assert items[1]["source_clock"] == "1970-01-01 08:00:00"


def test_nested_export_retains_leaf_paths_and_exact_bodies(pinned_clock_store):
    fixture = pinned_clock_store
    record = integration._record(fixture)
    clocks = WechatForwardedClocks(fixture.store).project(fixtures._rows(fixture))[fixture.html_parent]
    apply_forwarded_clock_evidence(record, clocks)
    first, second, *remaining = record["items"]
    wrapper = replace(
        first,
        stable_id="nested-source-wrapper",
        messaging={"forwarded_record": {"items": (first, second)}},
    )
    record["items"] = (wrapper, *remaining)
    items = forwarded_export_items(record)
    assert len(items) == 9
    assert items[0]["component_path"] == (1, 1)
    assert items[1]["component_path"] == (1, 2)
    assert items[2]["component_path"] == (2,)
    assert items[0]["text"] == first.content_text
    assert items[1]["text"] == second.content_text
    assert items[0]["timestamp_value"] == first.last_seen_at


def test_matched_preview_does_not_replace_the_original_export_leaf(pinned_clock_store):
    fixture = pinned_clock_store
    record = integration._record(fixture)
    clocks = WechatForwardedClocks(fixture.store).project(fixtures._rows(fixture))[fixture.html_parent]
    apply_forwarded_clock_evidence(record, clocks)
    first = record["items"][0]
    first.messaging["forwarded_record"] = {"items": ()}
    first.messaging["forwarded_preview_proof"] = {"status": "matched_excerpt"}
    items = forwarded_export_items(record)
    assert len(items) == 9
    assert items[0]["text"] == first.content_text
    assert items[0]["component_path"] == (1,)


def test_unlinked_packet_keeps_existing_export_shape_and_source_text(pinned_clock_store):
    fixture = pinned_clock_store
    fixture.clock_registry.write_text(json.dumps({"schema_version": 1, "links": []}))
    rows = _csv(fixture)
    parent = fixtures._rows(fixture)[0]
    assert any(row["Statement"] == parent["text"] for row in rows)
    children = json.loads(parent["provenance"])["forwarded_components"]
    assert not any(row["Statement"] == children[0]["text"] for row in rows)
    record = deepcopy(integration._record(fixture))
    assert forwarded_export_items(record) == ()


def test_registry_withdrawal_stops_a_pinned_export(pinned_clock_store):
    fixture = pinned_clock_store
    export = WechatSessionExport(fixture.store, _session(fixture))
    fixture.clock_registry.write_text(json.dumps({"schema_version": 1, "links": []}))
    with pytest.raises(WechatSessionSourceChanged, match="clock evidence changed"):
        list(export.items())
    assert export.connection is None


def test_download_routes_use_the_same_proved_child_clock(pinned_clock_store, tmp_path):
    fixture = pinned_clock_store
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
    session = _session(fixture)
    client = app.test_client()
    response = client.get(f"/browser/wechat/session/{session}/export.csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig"))))
    first = json.loads(fixtures._rows(fixture)[0]["provenance"])["forwarded_components"][0]
    assert any(
        row["Statement"] == first["text"] and row["Time"] == "2020-08-12 13:46:00"
        for row in rows
    )
    markdown = client.get(
        f"/browser/session/{session}/export", query_string={"source": "wechat"}
    )
    assert markdown.status_code == 200
    assert "#### Forwarded statements" in markdown.get_data(as_text=True)
    assert fixture.native_parent in markdown.get_data(as_text=True)
