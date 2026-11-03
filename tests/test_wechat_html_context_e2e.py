"""Verified HTML forwarded quotes retain literal envelopes and shared geometry.

Code version: v1.1.0-codex.0
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

from app.core.chat_history_browser import query_chat_history
from app.core.config import CrawlConfig
from app.core.wechat_contacts import (
    WechatContactRepository,
    contact_registry_path,
    initialize_contact_registry,
)
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_wechat_contacts import _make_forwarded_html_store


disposable_browser = test_sidebar_e2e.disposable_browser
TITLE = "Source forwarding title"
QUOTE = (
    "「 Leading label：Original text <script>window.wechatQuoteExecuted = true</script> "
    '<img src="https://invalid.example/quoted-media">」'
)
REPLY = (
    'A separate reply <img src="https://invalid.example/reply-media"> stays literal.'
)
SECOND = "Another original message shares the same time rail."
SENDER = "Original room-specific author"
CLOCKS = ("8 Aug 2021 12:34:56", "8 Aug 2021 12:35:04")


@pytest.fixture(scope="module")
def html_context_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-html-context")
    store = sandbox / "store"
    registry = contact_registry_path(store)
    registry.parent.mkdir(parents=True)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
    fixture = _make_forwarded_html_store(store)
    request = deepcopy(fixture["request"])
    quoted = deepcopy(request["forwarded_components"][0])
    quoted.update(
        text=REPLY,
        text_kind="quoted_reply",
        quoted_components=[{"kind": "rendered_quote", "text": QUOTE}],
    )
    plain = deepcopy(request["forwarded_components"][0])
    plain.update(
        export_msgid="42_component_2",
        ordinal=3,
        component_ordinal=2,
        raw_timestamp="2021-08-08 12:35:04",
        text=SECOND,
        text_kind="forwarded_rendered_text",
    )
    plain["source_locator"].update(message_ordinal=3, page_message_ordinal=3)
    children = [quoted, plain]
    body = "\n\n".join(
        [TITLE]
        + [
            f"[Forwarded text: {child['sender_label']} | {child['raw_timestamp']}]\n"
            + "\n\n".join(
                [item["text"] for item in child.get("quoted_components", [])]
                + [child["text"]]
            )
            for child in children
        ]
    )
    history = fixture["history"]
    with sqlite3.connect(history) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (request["id"],)
            ).fetchone()[0]
        )
        provenance.update(
            export_msgid_namespace="MesLocalID",
            primary_text_sha256=hashlib.sha256(TITLE.encode()).hexdigest(),
            quoted_components=[],
            forwarded_components=children,
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
    projected = WechatContactRepository(store).resolve_forwarded_components([request])[
        request["id"]
    ]
    assert projected["status"] == "source_proof_verified"
    assert all(
        child["contact"]["identity_status"] == "verified"
        for child in projected["components"]
    )
    preserved = history.read_bytes(), registry.read_bytes()
    page = query_chat_history(
        store, source="wechat", session=request["conversation_id"]
    )
    assert len(page.items) == 1
    assert page.items[0].content_text == body
    assert page.items[0].messaging["provenance"] == provenance
    quote, sibling = page.items[0].messaging["forwarded_record"]["items"]
    assert quote.messaging["quoted_reply"]["speaker"] == "Quoted message"
    assert quote.messaging["quoted_reply"]["text"] == QUOTE
    assert quote.messaging["display_text"] == REPLY
    assert quote.author_label == sibling.author_label == SENDER
    assert quote.messaging["timestamp_label"] == CLOCKS[0]
    assert sibling.messaging["timestamp_label"] == CLOCKS[1]
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
    with app.test_client() as client:
        response = client.get(
            "/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "session": request["conversation_id"],
                }
            )
        )
        assert response.status_code == 200
        assert b"Quoted message" in response.data
        assert (
            b"&lt;script&gt;window.wechatQuoteExecuted = true&lt;/script&gt;"
            in response.data
        )
        assert (
            b"<script>window.wechatQuoteExecuted = true</script>" not in response.data
        )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}",
            session=request["conversation_id"],
            parent_id=request["id"],
            history=history,
            registry=registry,
            preserved=preserved,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
def test_verified_html_forwarded_quote_uses_literal_context_and_shared_message_geometry(
    disposable_browser, html_context_server, tmp_path, width
):
    fixture = html_context_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000}, reduced_motion="reduce"
    )
    page = context.new_page()
    errors, remote = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "request",
        lambda request: (
            remote.append(request.url) if "invalid.example" in request.url else None
        ),
    )
    try:
        page.goto(
            fixture.url
            + "/browser?"
            + urlencode(
                {"view": "text", "source": "wechat", "session": fixture.session}
            ),
            wait_until="domcontentloaded",
        )
        page.evaluate("() => document.fonts.ready")
        sidebar = page.locator("#sidebar_toggle")
        if sidebar.get_attribute("aria-expanded") == "true":
            sidebar.click()
        parent = page.locator("#wechat_message_" + fixture.parent_id)
        disclosure = parent.locator(":scope > details.wechat-forwarded")
        expect(disclosure).not_to_have_attribute("open", "")
        expect(disclosure.locator(".wechat-forwarded-body")).not_to_be_visible()
        expect(disclosure.locator(":scope > summary")).to_have_text("Chat history")
        caption = parent.locator(":scope > .wechat-history-source-text")
        expect(caption).to_have_text(TITLE)
        expect(caption).to_be_visible()
        expect(parent.get_by_text(TITLE, exact=True)).to_have_count(1)
        summary = disclosure.locator(":scope > summary")
        summary.focus()
        summary.press("Enter")
        expect(disclosure).to_have_attribute("open", "")
        children = disclosure.locator("[data-wechat-embedded-row]")
        expect(children).to_have_count(2)
        first = children.nth(0)
        quote = first.locator("blockquote.wechat-message-quote")
        expect(quote.locator(".wechat-quoted-speaker")).to_have_text("Quoted message")
        expect(quote.locator(".wechat-quoted-text")).to_have_text(QUOTE)
        expect(first.locator(".wechat-message-text")).to_have_text(REPLY)
        expect(children.nth(1).locator(".wechat-message-text")).to_have_text(SECOND)
        expect(children.locator(".wechat-speaker")).to_have_text([SENDER, SENDER])
        expect(children.locator(":scope > time")).to_have_text(list(CLOCKS))
        expect(disclosure.locator("img,script,iframe,video,audio")).to_have_count(0)
        expect(page.locator("[data-chat-message-id]")).to_have_count(1)
        assert page.evaluate("window.wechatQuoteExecuted === true") is False
        assert not remote
        page.wait_for_function(
            "() => [...document.querySelectorAll('[data-wechat-embedded-row]')].every(row => row.style.getPropertyValue('--wechat-time-top'))"
        )
        geometry = disclosure.evaluate("""details => {
            const mainClock = details.closest('.wechat-message-row')
                .querySelector(':scope > time').getBoundingClientRect();
            const rows = [...details.querySelectorAll('[data-wechat-embedded-row]')];
            const clocks = rows.map(row => {
                const time = row.querySelector(':scope > time').getBoundingClientRect();
                const slot = row.querySelector('.wechat-time-slot').getBoundingClientRect();
                return {right: time.right, baselineError: Math.abs(time.top - slot.top)};
            });
            const body = details.querySelector('.wechat-forwarded-body');
            const style = getComputedStyle(body);
            const port = document.querySelector('[data-chat-scrollport]');
            return {clocks,mainClockRight:mainClock.right,
                radius:style.borderRadius,background:style.backgroundColor,
                border:parseFloat(style.borderTopWidth),shadow:style.boxShadow,
                physicalSurface:body.hasAttribute('data-chat-physical-surface'),
                blur:style.backdropFilter || style.webkitBackdropFilter,
                documentOverflow: document.documentElement.scrollWidth - innerWidth,
                listOverflow: port.scrollWidth - port.clientWidth};
        }""")
        assert geometry["radius"] == "0px"
        assert geometry["background"] == "rgba(0, 0, 0, 0)"
        assert geometry["border"] == 0
        assert geometry["shadow"] == geometry["blur"] == "none"
        assert not geometry["physicalSurface"]
        assert geometry["documentOverflow"] == geometry["listOverflow"] == 0
        assert max(clock["baselineError"] for clock in geometry["clocks"]) <= 1
        assert abs(geometry["clocks"][0]["right"] - geometry["clocks"][1]["right"]) <= 1
        assert (
            max(
                abs(clock["right"] - geometry["mainClockRight"])
                for clock in geometry["clocks"]
            )
            <= 1
        )
        assert not errors
        page.screenshot(path=tmp_path / f"html-context-{width}.png")
        summary.press("Space")
        expect(disclosure).not_to_have_attribute("open", "")
        assert fixture.preserved == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
        )
    finally:
        context.close()
