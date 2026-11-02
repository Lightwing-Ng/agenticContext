"""Published HTML forwarding reuses the collapsed transcript and message rows.

Code version: v1.0.4-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.config import CrawlConfig
from app.core.wechat_contacts import (
    WechatContactRepository,
    canonical_contact_id,
    contact_registry_path,
    initialize_contact_registry,
)
from app.core.wechat_display_names import display_names_path
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.wechat_forwarded_fixtures import make_forwarded_html_store


disposable_browser = test_sidebar_e2e.disposable_browser
VIEWER_NAME = "Canonical original participant"
TITLE = "Forward history"
TEXTS = (
    "Recovered card <img src=https://invalid.example/track> remains literal.\n"
    + "https://example.invalid/"
    + "segment/" * 30,
    "A second statement from the same original contact.",
    "A third statement from another original contact.",
)
RAW_CLOCKS = (
    "2021-08-08 12:34:56",
    "2021-08-08 12:35:04",
    "2021-08-08 12:36:08",
)
CLOCKS = (
    "8 Aug 2021 12:34:56",
    "8 Aug 2021 12:35:04",
    "8 Aug 2021 12:36:08",
)


@pytest.fixture(scope="module")
def html_forwarded_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-html-forwarded-browser")
    store = sandbox / "store"
    registry = contact_registry_path(store)
    registry.parent.mkdir(parents=True)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
    fixture = make_forwarded_html_store(store)
    history = fixture["history"]
    request = deepcopy(fixture["request"])
    original = request["forwarded_components"][0]
    children = []
    for index, (text, clock) in enumerate(zip(TEXTS, RAW_CLOCKS, strict=True)):
        child = deepcopy(original)
        sender = "wxid_forwarded" if index < 2 else "wxid_other_forwarded"
        child.update(
            export_msgid=f"42_component_{index + 1}",
            ordinal=index + 2,
            component_ordinal=index + 1,
            sender_native_id=sender,
            sender_label=(
                "First original source name"
                if index == 0
                else "Another remark for the same contact"
                if index == 1
                else "Third original participant"
            ),
            avatar_path=f"Room_files/Portrait/{sender}.jpg",
            raw_timestamp=clock,
            text=text,
            text_kind="rendered_forwarded_app_metadata"
            if index == 0
            else "forwarded_rendered_text",
        )
        child["source_locator"].update(
            message_ordinal=index + 2, page_message_ordinal=index + 2
        )
        children.append(child)
    body = "\n\n".join(
        [TITLE]
        + [
            f"[Forwarded text: {child['sender_label']} | {child['raw_timestamp']}]\n"
            + child["text"]
            for child in children
        ]
    )
    with sqlite3.connect(history) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (request["id"],)
            ).fetchone()[0]
        )
        provenance.update(
            export_msgid_namespace="MesLocalID",
            forwarded_components=children,
            primary_text_sha256=hashlib.sha256(TITLE.encode()).hexdigest(),
            quoted_components=[],
            synthetic_forwarded_fallback_displayed=False,
        )
        connection.execute(
            "UPDATE messages SET text=?,search_text=?,provenance=? WHERE id=?",
            (body, body.casefold(), json.dumps(provenance), request["id"]),
        )
    request.update(
        forwarded_components=children,
        text_sha256=hashlib.sha256(body.encode()).hexdigest(),
    )
    projection = WechatContactRepository(store).resolve_forwarded_components([request])[
        request["id"]
    ]
    assert projection["status"] == "source_proof_verified"
    contacts = [child["contact"] for child in projection["components"]]
    assert all(contact["identity_status"] == "verified" for contact in contacts)
    assert contacts[0]["id"] == contacts[1]["id"] != contacts[2]["id"]
    contact_id = canonical_contact_id(
        "wxid_forwarded", authority="native_message_sender"
    )
    assert contacts[0]["id"] == contact_id
    names = display_names_path(store)
    names.write_text(
        json.dumps({"schema_version": 1, "contacts": {contact_id: VIEWER_NAME}})
    )
    preserved = (history.read_bytes(), registry.read_bytes(), names.read_bytes())
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            store,
            computer_use_settings_path=sandbox / "agent.json",
            computer_use_runtime_root=sandbox / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    query = urlencode(
        {
            "source": "wechat",
            "account": "account:" + request["account_id"],
            "chat_kind": "group",
        }
    )
    export_path = "/browser/session/" + request["conversation_id"] + "/export?" + query
    with app.test_client() as client:
        export = client.get(export_path)
        assert export.status_code == 200
        preserved_export = export.data
        assert body.encode() in preserved_export
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}",
            app=app,
            session=request["conversation_id"],
            parent_id=request["id"],
            store=store,
            history=history,
            registry=registry,
            names=names,
            preserved=preserved,
            export_path=export_path,
            preserved_export=preserved_export,
            contact_id=contact_id,
            owner=request["account_id"],
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _open_html_forwarded(page, fixture):
    page.goto(
        fixture.url
        + "/browser?"
        + urlencode(
            {
                "view": "text",
                "source": "wechat",
                "account": "account:" + fixture.owner,
                "session": fixture.session,
            }
        ),
        wait_until="domcontentloaded",
    )
    page.evaluate("() => document.fonts.ready")
    sidebar = page.locator("#sidebar_toggle")
    if sidebar.get_attribute("aria-expanded") == "true":
        sidebar.click()
    card = page.locator("#wechat_message_" + fixture.parent_id)
    disclosure = card.locator(":scope > details.wechat-forwarded")
    expect(disclosure).to_have_count(1)
    expect(disclosure.locator(":scope > summary")).to_have_text(TITLE)
    return card, disclosure


def _assert_preserved(fixture):
    assert fixture.preserved == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        fixture.names.read_bytes(),
    )
    with fixture.app.test_client() as client:
        export = client.get(fixture.export_path)
        assert export.status_code == 200
        assert export.data == fixture.preserved_export


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_html_forwarded_source_uses_existing_collapsed_transcript(
    disposable_browser, html_forwarded_server, tmp_path, width, color_scheme
):
    fixture = html_forwarded_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        card, disclosure = _open_html_forwarded(page, fixture)
        expect(disclosure).not_to_have_attribute("open", "")
        expect(disclosure.locator(".wechat-forwarded-glass")).not_to_be_visible()
        expect(page.locator("[data-chat-message-id]")).to_have_count(1)
        expect(card.locator(":scope > .wechat-message-text")).to_have_count(0)
        summary = disclosure.locator(":scope > summary")
        summary.focus()
        summary.press("Enter")
        expect(disclosure).to_have_attribute("open", "")
        children = disclosure.locator("[data-wechat-embedded-row]")
        expect(children).to_have_count(3)
        for index, text in enumerate(TEXTS):
            expect(children.nth(index).locator(".wechat-message-text")).to_have_text(
                text
            )
            expect(children.nth(index).locator(".wechat-speaker")).to_have_text(
                VIEWER_NAME if index < 2 else "Third original participant"
            )
        expect(disclosure.locator("img, iframe, video, audio")).to_have_count(0)
        assert "[Forwarded text:" not in card.inner_text()
        page.wait_for_function(
            """() => [...document.querySelectorAll('[data-wechat-embedded-row]')]
                .every(row => row.style.getPropertyValue('--wechat-time-top'))"""
        )
        times = children.locator(":scope > time")
        expect(times).to_have_text(list(CLOCKS))
        expect(card.locator("xpath=..").locator(":scope > time")).to_have_text(
            "8 Aug 2021 13:00:00"
        )
        assert set(times.evaluate_all("nodes => nodes.map(node => node.title)")) == {
            "Source clock; timezone unknown"
        }
        geometry = disclosure.evaluate(
            """details => {
                const rows = [...details.querySelectorAll('[data-wechat-embedded-row]')];
                const clocks = rows.map(row => {
                    const article = row.querySelector(':scope > article');
                    const time = row.querySelector(':scope > time');
                    const slot = article.querySelector('.wechat-time-slot');
                    return {right: time.getBoundingClientRect().right,
                        topError: Math.abs(time.getBoundingClientRect().top - slot.getBoundingClientRect().top),
                        speakerSize: getComputedStyle(article.querySelector('.wechat-speaker')).fontSize,
                        bodySize: getComputedStyle(article.querySelector('.wechat-message-text')).fontSize};
                });
                const style = getComputedStyle(details.querySelector('.wechat-forwarded-glass'));
                const port = document.querySelector('[data-chat-scrollport]');
                const sourceHeaders = [details.closest('.wechat-message-row'), ...rows].map(row => {
                    const summary = row.querySelector(':scope > article > .wechat-provenance > summary');
                    const speaker = summary.querySelector('.wechat-speaker').getBoundingClientRect();
                    const clock = row.querySelector(':scope > time').getBoundingClientRect();
                    return {chevronDisplay: getComputedStyle(summary, '::after').display,
                        clockOverlapsSpeaker: clock.left < speaker.right && clock.right > speaker.left
                            && clock.top < speaker.bottom && clock.bottom > speaker.top};
                });
                return {clocks, sourceHeaders, radius: style.borderRadius,
                    blur: style.backdropFilter || style.webkitBackdropFilter,
                    documentOverflow: document.documentElement.scrollWidth - innerWidth,
                    listOverflow: port.scrollWidth - port.clientWidth};
            }"""
        )
        assert all(header["chevronDisplay"] == "none" for header in geometry["sourceHeaders"])
        assert not any(header["clockOverlapsSpeaker"] for header in geometry["sourceHeaders"])
        assert geometry["radius"] == "10px"
        assert "blur(" in geometry["blur"]
        assert geometry["documentOverflow"] == geometry["listOverflow"] == 0
        assert max(clock["topError"] for clock in geometry["clocks"]) <= 1
        assert (
            max(clock["right"] for clock in geometry["clocks"])
            - min(clock["right"] for clock in geometry["clocks"])
            <= 1
        )
        assert all(clock["speakerSize"] == "17px" for clock in geometry["clocks"])
        assert all(clock["bodySize"] == "15px" for clock in geometry["clocks"])
        (tmp_path / "html-forwarded-geometry.json").write_text(
            json.dumps(geometry, indent=2) + "\n"
        )
        page.screenshot(path=tmp_path / f"html-forwarded-{width}-{color_scheme}.png")
        summary.press("Space")
        expect(disclosure).not_to_have_attribute("open", "")
        expect(disclosure.locator(".wechat-forwarded-glass")).not_to_be_visible()
        assert not errors
        _assert_preserved(fixture)
    finally:
        context.close()


def test_html_forwarded_details_remain_usable_without_javascript(
    disposable_browser, html_forwarded_server
):
    fixture = html_forwarded_server
    context = disposable_browser.new_context(java_script_enabled=False)
    page = context.new_page()
    try:
        _, disclosure = _open_html_forwarded(page, fixture)
        expect(disclosure.locator(".wechat-forwarded-glass")).not_to_be_visible()
        disclosure.locator(":scope > summary").press("Enter")
        expect(disclosure.locator(".wechat-forwarded-glass")).to_be_visible()
        children = disclosure.locator("[data-wechat-embedded-row]")
        expect(children).to_have_count(3)
        assert children.locator(".wechat-message-text").all_text_contents() == list(
            TEXTS
        )
        assert "[Forwarded text:" not in disclosure.inner_text()
        _assert_preserved(fixture)
    finally:
        context.close()
