"""Conversation unions preserve filtered recall, source scopes, and saved anchors.

Code version: v1.0.0-codex.0
"""

from threading import Thread
from unittest.mock import patch
import json
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.chat_history_browser import query_chat_history
from app.core.config import CrawlConfig
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests import test_wechat_conversation_events as event_fixtures
from tests.test_wechat_contacts import bind


event_store = event_fixtures.event_store
disposable_browser = test_sidebar_e2e.disposable_browser


@pytest.fixture
def conversation_store(event_store):
    fixture = event_store
    with sqlite3.connect(fixture.registry) as connection:
        for sender in (event_fixtures.OWNER, event_fixtures.PEER):
            identifier = connection.execute(
                "SELECT id FROM contacts WHERE username=?", (sender,)
            ).fetchone()[0]
            bind(connection, identifier, source=event_fixtures.NATIVE, sender=sender)
    return fixture


def test_union_counts_pages_and_searches_every_retained_observation(conversation_store):
    fixture = conversation_store
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.execute(
            "UPDATE messages SET search_text=search_text || ' html-only-alias' WHERE id=?",
            (fixture.ids[0][0],),
        )
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    page = query_chat_history(
        fixture.store, source="wechat", session=event_fixtures.SESSION, page_size=2
    )
    assert page.total_count == page.current_session.message_count == 3
    assert page.total_pages == 2
    assert [item.stable_id for item in page.items] == [
        pair[1] for pair in fixture.ids[:2]
    ]
    assert all(len(item.messaging["source_observations"]) == 2 for item in page.items)
    assert all(item.messaging["source_label"] == "iPad archive" for item in page.items)
    assert all(
        item.messaging["contact"]["identity_status"] == "verified"
        for item in page.items
    )
    found = query_chat_history(
        fixture.store,
        source="wechat",
        session=event_fixtures.SESSION,
        query="html-only-alias",
    )
    assert found.total_count == 1
    assert found.items[0].stable_id == fixture.ids[0][1]
    listing = query_chat_history(fixture.store, source="wechat")
    assert listing.sessions[0].message_count == 3
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )


def test_one_valid_routed_row_cannot_authorize_a_forged_row_in_the_same_snapshot(
    conversation_store,
):
    fixture = conversation_store
    forged = fixture.ids[0][0]
    with sqlite3.connect(fixture.repository.path) as connection:
        raw = connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (forged,)
        ).fetchone()[0]
        provenance = json.loads(raw)
        provenance["source_snapshot"] = 2
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(provenance), forged),
        )
    page = query_chat_history(
        fixture.store, source="wechat", session=event_fixtures.SESSION
    )
    rows = {item.stable_id: item.messaging for item in page.items}
    assert rows[forged]["partition_id"] == event_fixtures.HTML
    assert rows[forged]["source_label"] == "HTML export"
    assert rows[forged]["sender_identifier_namespace"] == "unverified_export_field"
    assert rows[fixture.ids[0][1]]["partition_id"] == event_fixtures.NATIVE
    assert rows[fixture.ids[0][1]]["source_label"] == "iPad archive"
    assert rows[fixture.ids[0][1]]["sender_identifier_namespace"] == "native_user_name"


def test_preferred_native_event_retains_its_verified_export_contact(event_store):
    page = query_chat_history(
        event_store.store, source="wechat", session=event_fixtures.SESSION
    )
    assert len(page.items) == 3
    assert all(
        item.messaging["contact"]["identity_status"] == "verified"
        for item in page.items
    )
    assert all(
        item.messaging["viewer_display_name"] == "Source label" for item in page.items
    )
    assert all(
        item.messaging["contact"]["resolution_reason"]
        == "verified_conversation_event_sources"
        for item in page.items
    )


def test_native_username_label_keeps_the_verified_export_viewing_name(
    conversation_store,
):
    fixture = conversation_store
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.executemany(
            "UPDATE messages SET sender_label=sender_id WHERE id=?",
            [(native,) for _html, native in fixture.ids],
        )
    page = query_chat_history(
        fixture.store, source="wechat", session=event_fixtures.SESSION
    )
    assert all(item.author_label == item.messaging["sender_id"] for item in page.items)
    assert all(
        item.messaging["viewer_display_name"] == "Source label" for item in page.items
    )
    assert all(
        item.messaging["contact"]["identity_status"] == "verified"
        for item in page.items
    )


@pytest.fixture
def conversation_server(conversation_store, tmp_path):
    fixture = conversation_store
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
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
def test_saved_source_hash_and_missing_page_return_to_one_preferred_bubble(
    disposable_browser,
    conversation_server,
    width,
):
    base, fixture = conversation_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000}, reduced_motion="reduce"
    )
    page = context.new_page()
    old, preferred = fixture.ids[-1]
    url = base + "/browser?view=text&source=wechat&session=" + event_fixtures.SESSION
    try:
        page.goto(url + "#wechat_message_" + old, wait_until="domcontentloaded")
        target = page.locator("#wechat_message_" + preferred)
        expect(target).to_be_visible()
        expect(page.locator("article.wechat-message")).to_have_count(3)
        alias = page.locator("#wechat_message_" + old)
        expect(alias).to_have_attribute("data-wechat-source-alias", "")
        expect(
            target.locator(":scope > details.wechat-provenance")
        ).not_to_have_attribute("open", "")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.goto(
            url + "&q=body%200#wechat_message_" + old, wait_until="domcontentloaded"
        )
        expect(page).to_have_url(
            re.compile(r"/browser\?.*#wechat_message_" + preferred + "$")
        )
        query = parse_qs(urlsplit(page.url).query)
        assert query["session"] == [event_fixtures.SESSION]
        assert query["account"] == ["account:" + event_fixtures.OWNER]
        assert "q" not in query
        expect(page.locator("#wechat_message_" + preferred)).to_be_visible()
    finally:
        context.close()
