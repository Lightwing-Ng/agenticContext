"""Contact evidence shows clock conflicts without choosing a canonical source time.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core import wechat_contact_analysis as analysis
from app.core.config import CrawlConfig
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests import test_wechat_contact_statement_groups as grouping
from tests.test_wechat_contacts import bind, insert_contact


CID = grouping.CID
OTHER = "wxid_other_clock_contact"
statement_store = grouping.statement_store
disposable_browser = test_sidebar_e2e.disposable_browser


@pytest.mark.parametrize(("clock", "basis", "epoch", "conflict", "uncertain"), [
    ("2026-10-30T05:45:23Z", "UTC+00:00", grouping.EPOCH, False, False),
    ("2026-10-30T13:45:23", "Source clock; timezone unknown", grouping.EPOCH, False, True),
    ("2026-10-30T18:00:44+08:00", "UTC+08:00", grouping.EPOCH + 15321, True, False),
    ("2026-10-30T13:45:23+08:00", "UTC+08:00", grouping.EPOCH + 1, True, False),
    ("2026-10-30T13:45:23+08:00", "UTC+08:00", 0, False, True),
    ("2026-10-30T13:45:23+08:00", "UTC+08:00", True, False, True),
    ("2026-10-30T13:45:23+08:00", "UTC+08:00", "1e100", False, True),
    ("2026-10-30T13:45:23+08:00", "UTC+08:00", None, False, False),
    ("2026-10-30T13:45:23+08:00", "UTC+08:00", "", False, False),
    ("", "Unavailable source time", None, False, True),
])
def test_clock_comparison_keeps_raw_observations_and_primary_transport(
    statement_store, clock, basis, epoch, conflict, uncertain
):
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        message = fixture.rows[1]["id"]
        provenance = json.loads(connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (message,)
        ).fetchone()[0])
        provenance["unix_timestamp"] = epoch
        connection.execute(
            "UPDATE messages SET timestamp_value=?,time_basis=?,provenance=? WHERE id=?",
            (clock, basis, json.dumps(provenance), message),
        )
        raw_clocks = dict(connection.execute(
            "SELECT id,timestamp_value FROM messages WHERE sender_id=?", (grouping.TARGET,)
        ))
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()

    result = analysis.query_contact_analysis(fixture.store, CID)

    assert result["total_count"] == 1
    item, = result["items"]
    assert item["time_conflict"] is conflict
    assert item["time_uncertain"] is uncertain
    assert item["timestamp_value"] == raw_clocks[item["id"]]
    assert {origin["id"]: origin["raw_timestamp_value"] for origin in item["observations"]} == (
        raw_clocks
    )
    selected = next(origin for origin in item["observations"] if origin["id"] == message)
    assert selected["native_unix_timestamp"] == epoch
    assert selected["time_basis"] == basis
    assert "source_clock" in selected
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("epoch", [None, ""])
def test_missing_native_epoch_keeps_single_aware_observation_certain(statement_store, epoch):
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        message = fixture.rows[0]["id"]
        provenance = json.loads(connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (message,)
        ).fetchone()[0])
        provenance["unix_timestamp"] = epoch
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?", (json.dumps(provenance), message)
        )
        connection.execute(
            "UPDATE messages SET sender_id='wxid_other' WHERE id=?",
            (fixture.rows[1]["id"],),
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()

    result = analysis.query_contact_analysis(fixture.store, CID)

    assert result["total_count"] == 1
    item, = result["items"]
    assert item["observation_count"] == 1
    assert not item["time_conflict"] and not item["time_uncertain"]
    assert item["timestamp_value"] == "2026-10-30T13:45:23+08:00"
    origin, = item["observations"]
    assert origin["native_clock_present"]
    assert origin["native_unix_timestamp"] == epoch
    assert not origin["clock_inconsistent"] and not origin["clock_uncertain"]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.fixture
def conflicting_contact_server(statement_store, tmp_path):
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        second = fixture.rows[1]["id"]
        provenance = json.loads(connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (second,)
        ).fetchone()[0])
        provenance["unix_timestamp"] = grouping.EPOCH + 15321
        connection.execute(
            "UPDATE messages SET timestamp_value=?,timestamp_label=?,time_sort=?,"
            "provenance=? WHERE id=?",
            (
                "2026-10-30T18:00:44+08:00", "30 Oct 2026 18:00:44",
                grouping.EPOCH + 15321, json.dumps(provenance), second,
            ),
        )
        other = dict(connection.execute(
            "SELECT * FROM messages WHERE id=?", (fixture.rows[0]["id"],)
        ).fetchone())
        other.update(
            id=hashlib.sha256(b"independent-contact-unknown-clock").hexdigest(),
            sender_id=OTHER,
            sender_label="Independent contact",
            text="Independent contact with unavailable time",
            search_text="independent contact with unavailable time",
            timestamp_value="1970-01-01T00:00:00+00:00",
            timestamp_label="1 Jan 1970 00:00:00",
            time_sort=0,
            time_basis="Unavailable source time",
            quality_flags='["possible_epoch_zero_placeholder"]',
        )
        provenance = json.loads(other["provenance"])
        provenance["unix_timestamp"] = 0
        other["provenance"] = json.dumps(provenance)
        columns = ",".join(other)
        marks = ",".join("?" for _ in other)
        connection.execute(
            f"INSERT INTO messages({columns}) VALUES({marks})", tuple(other.values())
        )
        other.update(
            id=hashlib.sha256(b"independent-contact-unresolved-clock").hexdigest(),
            text="Independent contact with unresolved source clock",
            search_text="independent contact with unresolved source clock",
            timestamp_value="2026-10-30T13:45:23+08:00",
            timestamp_label="30 Oct 2026 13:45:23",
            time_sort=grouping.EPOCH,
            time_basis="UTC+08:00",
            quality_flags="[]",
        )
        connection.execute(
            f"INSERT INTO messages({columns}) VALUES({marks})", tuple(other.values())
        )
    with sqlite3.connect(fixture.registry) as connection:
        other_contact = insert_contact(connection, OTHER)
        bind(connection, other_contact, source=fixture.rows[0]["source_id"], sender=OTHER)
    prepared = analysis.query_contact_analysis(fixture.store, CID)
    assert prepared["total_count"] == 1
    assert prepared["items"][0]["time_conflict"]
    other_page = analysis.query_contact_analysis(fixture.store, other_contact)
    assert other_page["total_count"] == 2
    assert all(not item["time_conflict"] for item in other_page["items"])
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    with patch(
        "app.web.app.SavedConfigStore.load", return_value=SavedConfigStore(CrawlConfig())
    ):
        app = create_app(
            fixture.store,
            computer_use_settings_path=tmp_path / "agent.json",
            computer_use_runtime_root=tmp_path / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            **vars(fixture), url=f"http://127.0.0.1:{server.server_port}",
            other_contact=other_contact, before=before,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(("width", "scheme"), [(788, "light"), (390, "dark")])
def test_conflicts_expand_all_source_times_and_other_contact_stays_independent(
    disposable_browser, conflicting_contact_server, tmp_path, width, scheme
):
    fixture = conflicting_contact_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        reduced_motion="reduce", color_scheme=scheme,
    )
    page = context.new_page()
    try:
        page.goto(
            fixture.url + "/browser/wechat/contacts/" + CID, wait_until="networkidle"
        )
        if page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "true":
            page.locator("#sidebar_toggle").click()
        rows = page.locator("tr[data-contact-statement-id]")
        expect(rows).to_have_count(1)
        time_cell = rows.locator("td").nth(1)
        expect(time_cell).to_have_text("Conflicting source times")
        expect(time_cell.locator("[datetime]")).to_have_count(0)
        sources = rows.locator("details[data-wechat-contact-sources]")
        assert sources.get_attribute("open") is None
        assert sources.evaluate("node => node.closest('td').cellIndex") == 2
        sources.locator("summary").focus()
        sources.locator("summary").press("Enter")
        expect(sources).to_have_attribute("open", "")
        observations = sources.locator("[data-wechat-contact-observation]")
        expect(observations).to_have_count(2)
        assert observations.first.bounding_box()["width"] >= 100
        clock_note = observations.first.locator("[data-wechat-contact-observation-time]")
        assert clock_note.bounding_box()["height"] <= 80
        assert set(observations.locator("time").evaluate_all(
            "nodes => nodes.map(node => node.getAttribute('datetime'))"
        )) == {"2026-10-30T13:45:23+08:00", "2026-10-30T18:00:44+08:00"}
        assert set(observations.evaluate_all(
            "nodes => nodes.map(node => node.dataset.contactAccountKey)"
        )) == {row["account_key"] for row in fixture.rows}
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=tmp_path / f"contact-clock-conflicts-{width}-{scheme}.png")

        page.goto(
            fixture.url + "/browser/wechat/contacts/" + fixture.other_contact,
            wait_until="networkidle",
        )
        other_rows = page.locator("tr[data-contact-statement-id]")
        expect(other_rows).to_have_count(2)
        unknown = other_rows.filter(has_text="Independent contact with unavailable time")
        expect(unknown.locator("td").nth(1)).to_have_text("Unknown time")
        expect(unknown.locator("td").nth(1).locator("[datetime]")).to_have_count(0)
        unresolved = other_rows.filter(has_text="Independent contact with unresolved source clock")
        expect(unresolved.locator("td").nth(1)).to_have_text("Unresolved source times")
        expect(unresolved.locator("td").nth(1).locator("[datetime]")).to_have_count(0)
        expect(page.locator("[data-wechat-contact-time-conflict]")).to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert fixture.before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    finally:
        context.close()
