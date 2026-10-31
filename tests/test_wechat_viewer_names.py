"""Viewer names project across native identities without rewriting source evidence.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
from html import unescape
import json
import re
import sqlite3
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.config import CrawlConfig
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_contacts import (
    canonical_contact_id,
    contact_observation,
    contact_registry_path,
    initialize_contact_registry,
)
from app.core.wechat_display_names import display_names_path
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_wechat_archive import make_archive


OWNER_A = "wxid_account_a"
OWNER_B = "wxid_account_b"
OTHER = "wxid_peer"
ALIAS = "Local <viewer> & 昵称"
RAW_SPEAKER = "Synthetic source phone label"
RAW_ACCOUNT = "Synthetic source account · ount_a"
disposable_browser = test_sidebar_e2e.disposable_browser


def _source(owner):
    return "ios-" + hashlib.md5(owner.encode()).hexdigest()


def _session(store, owner):
    return next(
        session
        for session in query_chat_history(
            store, source="wechat", account=f"account:{owner}"
        ).sessions
        if session.messaging["kind"] == "direct"
    )


def _write_preference(store, contact_id, name=ALIAS):
    path = display_names_path(store)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "contacts": {contact_id: name} if name is not None else {},
            },
            ensure_ascii=False,
        )
    )


def _query(fixture, owner):
    session = fixture.sessions[owner]
    return query_chat_history(
        fixture.store,
        source="wechat",
        account=f"account:{owner}",
        session=session.stable_id,
    )


@pytest.fixture
def viewer_archive(tmp_path):
    archive = make_archive(tmp_path / "archive", repeated=3)
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    sessions = {owner: _session(store, owner) for owner in (OWNER_A, OWNER_B)}
    history = wechat_archive_path(store)
    with sqlite3.connect(history) as connection:
        connection.execute(
            "UPDATE partitions SET account_label=? WHERE id=?",
            (RAW_ACCOUNT, _source(OWNER_A)),
        )
        connection.execute(
            "UPDATE messages SET sender_label=? WHERE sender_id=?",
            (RAW_SPEAKER, OWNER_A),
        )
        b_messages = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal",
                (sessions[OWNER_B].stable_id,),
            )
        ]
        connection.execute(
            "UPDATE messages SET sender_id=?,sender_label=?,direction='received' WHERE id=?",
            (OWNER_A, RAW_SPEAKER, b_messages[0]),
        )
        connection.execute(
            "UPDATE messages SET sender_label=? WHERE id=?",
            (RAW_SPEAKER, b_messages[-1]),
        )
        unknown = connection.execute(
            "SELECT m.id,c.id FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "WHERE c.partition_id='xlsx-2' ORDER BY m.ordinal LIMIT 1"
        ).fetchone()
        connection.execute(
            "UPDATE messages SET sender_id=?,sender_label=? WHERE id=?",
            (OWNER_A, RAW_SPEAKER, unknown[0]),
        )
    registry = contact_registry_path(store)
    contact_id = canonical_contact_id(OWNER_A, authority="native_account_owner")
    other_id = canonical_contact_id(OTHER, authority="native_contact_username")
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        for identifier, username in ((contact_id, OWNER_A), (other_id, OTHER)):
            connection.execute(
                "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
                (identifier, "wechat", "", username, "wechat_account", "verified"),
            )
            connection.execute(
                "INSERT INTO contact_identifiers VALUES(?,?,?,?,?)",
                (
                    identifier,
                    "native_username",
                    username,
                    "",
                    "native_contact_username",
                ),
            )
        for source in (_source(OWNER_A), _source(OWNER_B)):
            for identifier, sender in ((contact_id, OWNER_A), (other_id, OTHER)):
                connection.execute(
                    "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                    (source, sender, "", "", identifier, "native_message_sender", "{}"),
                )
        for owner, remark in (
            (OWNER_A, "Account A remark"),
            (OWNER_B, "Account B remark"),
        ):
            observation = contact_observation(
                contact_id,
                source_id=_source(owner),
                owner_account_id=owner,
                kind="remark",
                value=remark,
            )
            columns = tuple(observation)
            connection.execute(
                "INSERT INTO contact_observations ("
                + ",".join(columns)
                + ") VALUES("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(observation[column] for column in columns),
            )
    _write_preference(store, contact_id)
    return SimpleNamespace(
        store=store,
        sessions=sessions,
        contact_id=contact_id,
        other_id=other_id,
        history=history,
        registry=registry,
        unknown_session=unknown[1],
        unknown_message=unknown[0],
        sandbox=tmp_path,
    )


def test_viewer_name_follows_canonical_identity_across_sources_without_data_changes(
    viewer_archive,
):
    fixture = viewer_archive
    before = (fixture.history.read_bytes(), fixture.registry.read_bytes())
    pages = [_query(fixture, owner) for owner in (OWNER_A, OWNER_B)]
    projected = [
        message
        for page in pages
        for message in page.items
        if message.messaging["sender_id"] == OWNER_A
    ]
    assert len(projected) == 2
    assert {message.messaging["contact"]["id"] for message in projected} == {
        fixture.contact_id
    }
    assert {message.messaging["viewer_display_name"] for message in projected} == {
        ALIAS
    }
    assert {message.author_label for message in projected} == {RAW_SPEAKER}
    assert {
        message.messaging["contact"]["source_display_name"] for message in projected
    } == {RAW_SPEAKER}
    assert {
        message.messaging["contact"]["preferred_name"] for message in projected
    } == {"Account A remark", "Account B remark"}
    for message in projected:
        assert message.messaging["contact"]["viewer_display_name"] == ALIAS
        assert message.messaging["sender_id"] == OWNER_A
    other = next(
        message for message in pages[1].items if message.messaging["sender_id"] == OTHER
    )
    assert other.author_label == RAW_SPEAKER
    assert other.messaging["contact"]["id"] == fixture.other_id
    assert not other.messaging.get("viewer_display_name")
    unknown = query_chat_history(
        fixture.store, source="wechat", session=fixture.unknown_session
    )
    unverified = next(
        message
        for message in unknown.items
        if message.stable_id == fixture.unknown_message
    )
    assert unverified.messaging["sender_id"] == OWNER_A
    assert unverified.author_label == RAW_SPEAKER
    assert unverified.messaging["contact"]["identity_status"] == "unresolved"
    assert not unverified.messaging.get("viewer_display_name")
    assert pages[0].current_session.messaging["account_label"] == RAW_ACCOUNT
    assert (
        pages[0].current_session.messaging["viewer_account_label"]
        == ALIAS + " · ount_a"
    )
    option = next(
        account
        for account in pages[0].messaging["accounts"]
        if account["key"] == f"account:{OWNER_A}"
    )
    assert option["name"] == RAW_ACCOUNT
    assert option["viewer_name"] == ALIAS + " · ount_a"
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_viewer_preference_changes_and_clear_are_visible_on_the_next_query(
    viewer_archive,
):
    fixture = viewer_archive
    before = (fixture.history.read_bytes(), fixture.registry.read_bytes())

    def self_message():
        return next(
            message
            for message in _query(fixture, OWNER_A).items
            if message.messaging["sender_id"] == OWNER_A
        )

    first = self_message()
    assert first.messaging["viewer_display_name"] == ALIAS
    _write_preference(fixture.store, fixture.contact_id, "Revised viewer")
    assert self_message().messaging["viewer_display_name"] == "Revised viewer"
    assert first.messaging["viewer_display_name"] == ALIAS
    _write_preference(fixture.store, fixture.contact_id, None)
    assert not self_message().messaging.get("viewer_display_name")
    display_names_path(fixture.store).unlink()
    assert self_message().author_label == RAW_SPEAKER
    assert not self_message().messaging.get("viewer_display_name")
    assert not _query(fixture, OWNER_A).current_session.messaging[
        "viewer_account_label"
    ]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def _application(fixture):
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            fixture.store,
            computer_use_settings_path=fixture.sandbox / "agent.json",
            computer_use_runtime_root=fixture.sandbox / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    return app


def test_viewer_names_render_as_text_and_exports_keep_source_evidence(viewer_archive):
    fixture = viewer_archive
    before = (fixture.history.read_bytes(), fixture.registry.read_bytes())
    client = _application(fixture).test_client()
    session = fixture.sessions[OWNER_A]
    query = {
        "view": "text",
        "source": "wechat",
        "account": f"account:{OWNER_A}",
        "chat_kind": "direct",
        "session": session.stable_id,
    }
    response = client.get("/browser", query_string=query)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Local &lt;viewer&gt; &amp; 昵称" in html
    assert "<viewer>" not in html
    assert "<dt>Source display name</dt><dd>" + RAW_SPEAKER + "</dd>" in html
    assert f'value="account:{OWNER_A}" selected' in html
    assert fixture.contact_id in html
    links = [unescape(value) for value in re.findall(r'href="([^"]+)"', html)]
    export_url = next(link for link in links if "/export?" in link)
    assert parse_qs(urlsplit(export_url).query)["account"] == [f"account:{OWNER_A}"]
    exported = client.get(export_url)
    assert exported.status_code == 200
    markdown = exported.get_data(as_text=True)
    assert f"- Source account: {RAW_ACCOUNT}" in markdown
    assert f'- Viewer account label: "{ALIAS} · ount_a"' in markdown
    assert f'- Source display name: "{RAW_SPEAKER}"' in markdown
    assert f"- Sender ID: {OWNER_A}" in markdown
    assert f"- Contact ID: {fixture.contact_id}" in markdown
    assert f". {ALIAS} · " in markdown
    assert build_chat_history_markdown(_query(fixture, OWNER_A)) == markdown
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.fixture
def viewer_server(viewer_archive, monkeypatch):
    home = viewer_archive.sandbox / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    app = _application(viewer_archive)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            base_url=f"http://127.0.0.1:{server.server_port}", fixture=viewer_archive
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_browser_viewer_names_keep_native_filters_and_compact_source_details(
    disposable_browser, viewer_server, width, color_scheme
):
    fixture = viewer_server.fixture
    before = (fixture.history.read_bytes(), fixture.registry.read_bytes())
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(
            viewer_server.base_url
            + "/browser?"
            + urlencode({"view": "text", "source": "wechat", "chat_kind": "direct"}),
            wait_until="networkidle",
        )
        if (
            width < 600
            and page.locator("#sidebar_toggle").get_attribute("aria-expanded") != "true"
        ):
            page.locator("#sidebar_toggle").click()
        page.get_by_role(
            "button", name=re.compile(r"^Filter by WeChat account:")
        ).click()
        page.get_by_role("option", name=ALIAS + " · ount_a", exact=True).click()
        expect(page).to_have_url(
            re.compile(r"account=(?:account%3A|account:)wxid_account_a")
        )
        assert parse_qs(urlsplit(page.url).query)["account"] == [f"account:{OWNER_A}"]
        assert "Local" not in page.url
        expect(page.locator("#browser_wechat_account")).to_have_value(
            f"account:{OWNER_A}"
        )
        if (
            width < 600
            and page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "true"
        ):
            page.locator("#sidebar_toggle").click()
        page.get_by_role("link", name="Same title", exact=True).click()
        page.evaluate("document.fonts.ready")
        assert parse_qs(urlsplit(page.url).query)["account"] == [f"account:{OWNER_A}"]
        expect(
            page.locator(".wechat-conversation-context .wechat-account-label")
        ).to_have_text(ALIAS + " · ount_a")
        target = page.locator(".wechat-message--sent")
        expect(target).to_have_count(1)
        expect(target.locator(".wechat-speaker")).to_have_text(ALIAS)
        assert page.locator("viewer").count() == 0
        assert page.locator('img[src*="invalid.example"]').count() == 0
        details = target.locator("details.wechat-provenance")
        assert details.get_attribute("open") is None
        current_url = page.url
        details.locator("summary").click()
        raw_name = details.locator("dt").filter(
            has_text=re.compile(r"^Source display name$")
        )
        expect(raw_name.locator("xpath=following-sibling::dd[1]")).to_have_text(
            RAW_SPEAKER
        )
        sender = details.locator("dt").filter(has_text=re.compile(r"^Sender ID$"))
        expect(sender.locator("xpath=following-sibling::dd[1]")).to_have_text(OWNER_A)
        assert page.url == current_url
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1
        assert page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches") == (
            color_scheme == "dark"
        )
        export_link = page.get_by_role(
            "link", name="Export text", exact=True
        ).get_attribute("href")
        assert parse_qs(urlsplit(export_link).query)["account"] == [
            f"account:{OWNER_A}"
        ]
        exported = page.request.get(viewer_server.base_url + export_link)
        assert exported.ok
        assert f"- Source account: {RAW_ACCOUNT}" in exported.text()
        assert f'- Source display name: "{RAW_SPEAKER}"' in exported.text()
        assert errors == []
        assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    finally:
        context.close()
