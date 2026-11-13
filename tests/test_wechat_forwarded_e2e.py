"""Forwarded transcripts reuse native message bubbles without duplicating bodies.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

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
from app.core.wechat_archive import _identifier, wechat_archive_path
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_wechat_reconcile import merge, record, source
from tests.wechat_browser_markup import reload_browser_after_verification


disposable_browser = test_sidebar_e2e.disposable_browser
ORIGINALS = (
    "Original text <img src=https://invalid.example/track> remains literal.",
    "An original reply with a long URL: https://example.invalid/" + "segment/" * 30,
)


@pytest.fixture(scope="module")
def forwarded_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-forwarded")
    store = sandbox / "store"
    metadata = source()
    owner = metadata["source_account_id"]
    originals = [
        record(
            metadata,
            f"original-{index}",
            peer="original@chatroom",
            text=text,
            sender_id=owner if index == 0 else "wxid_original_peer",
            sender_display_name="Original owner"
            if index == 0
            else "Original participant",
            direction="sent" if index == 0 else "received",
            server_id=str(9000 + index),
            sent_at_utc_iso=f"2021-08-07T10:5{index}:03+00:00",
        )
        for index, text in enumerate(ORIGINALS)
    ]
    references = {
        "schema_version": 1,
        "authority": "user_confirmation_with_native_sequence_corroboration",
        "items": [
            {
                "kind": "message",
                "message_id": _identifier(metadata["source_id"], f"original-{index}"),
                "account_id": owner,
                "peer_id": "original@chatroom",
            }
            for index in range(len(originals))
        ],
    }
    parent = record(
        metadata,
        "forward-parent",
        sender_id=owner,
        sender_display_name="Forwarding owner",
        direction="sent",
        message_type=49,
        text_extraction_kind="forwarded_text",
        text="Forwarded conversation",
        forwarded_references=references,
        server_id="9999",
    )
    before = record(
        metadata,
        "before-forward",
        text="A preceding message.",
        server_id="9998",
        sent_at_utc_iso="2021-08-07T10:58:03+00:00",
    )
    after = record(
        metadata,
        "after-forward",
        text="A following message.",
        server_id="10000",
        sent_at_utc_iso="2021-08-07T11:00:03+00:00",
    )
    merge(store, [metadata], [*originals, before, parent, after])
    history = wechat_archive_path(store)
    parent_id = _identifier(metadata["source_id"], "forward-parent")
    with sqlite3.connect(history) as connection:
        session = connection.execute(
            "SELECT conversation_id FROM messages WHERE id=?", (parent_id,)
        ).fetchone()[0]
    preserved = history.read_bytes()
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
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}",
            session=session,
            parent_id=parent_id,
            history=history,
            preserved=preserved,
            owner=owner,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def open_forwarded(page, fixture):
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
    card = page.locator("#wechat_message_" + fixture.parent_id)
    disclosure = card.locator(":scope > details.wechat-forwarded")
    expect(disclosure).to_have_count(1)
    page.evaluate("() => document.fonts.ready")
    sidebar = page.locator("#sidebar_toggle")
    if sidebar.get_attribute("aria-expanded") == "true":
        sidebar.click()
    return card, disclosure


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_forwarded_messages_reuse_bubbles_and_main_clock_rail_without_a_panel(
    disposable_browser, forwarded_server, width, color_scheme
):
    fixture = forwarded_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        card, disclosure = open_forwarded(page, fixture)
        expect(disclosure).not_to_have_attribute("open", "")
        expect(disclosure.locator(".wechat-forwarded-body")).not_to_be_visible()
        expect(page.locator("[data-chat-message-id]")).to_have_count(3)
        summary = disclosure.locator(":scope > summary")
        expect(summary).to_have_text("Chat history")
        assert summary.get_attribute("title") is None
        expect(disclosure.locator("[data-wechat-transcript]")).to_have_attribute(
            "aria-label", "Chat history messages"
        )
        summary.focus()
        summary.press("Enter")
        expect(disclosure).to_have_attribute("open", "")
        children = disclosure.locator("[data-wechat-embedded-row]")
        expect(children).to_have_count(2)
        for index, text in enumerate(ORIGINALS):
            expect(children.nth(index).locator(".wechat-message-text")).to_have_text(
                text
            )
        expect(disclosure.locator("img, iframe, video, audio")).to_have_count(0)
        page.wait_for_function(
            """() => [...document.querySelectorAll('[data-wechat-embedded-row]')]
                .every(row => row.style.getPropertyValue('--wechat-time-top'))"""
        )
        geometry = disclosure.evaluate(
            """details => {
                const body = details.querySelector('.wechat-forwarded-body');
                const rows = [...details.querySelectorAll('[data-wechat-embedded-row]')];
                const clocks = rows.map(row => {
                    const bubble = row.querySelector(':scope > article');
                    const time = row.querySelector(':scope > time');
                    const slot = bubble.querySelector(':scope > details > summary .wechat-time-slot');
                    return {
                        right: time.getBoundingClientRect().right,
                        topError: Math.abs(time.getBoundingClientRect().top - slot.getBoundingClientRect().top),
                        bubble: getComputedStyle(bubble).borderRadius,
                        speakerSize: getComputedStyle(bubble.querySelector('.wechat-speaker')).fontSize,
                        bodySize: getComputedStyle(bubble.querySelector('.wechat-message-text')).fontSize,
                    };
                });
                const style = getComputedStyle(body);
                return {clocks, radius: style.borderRadius,
                    background: style.backgroundColor, border: style.borderTopWidth,
                    shadow: style.boxShadow,
                    physicalSurface: details.querySelector(':scope > .wechat-forwarded-body').hasAttribute('data-chat-physical-surface'),
                    referenceRight: details.closest('.wechat-message-row').querySelector(':scope > time').getBoundingClientRect().right,
                    blur: style.backdropFilter || style.webkitBackdropFilter,
                    overflow: document.documentElement.scrollWidth - innerWidth,
                    listOverflow: document.querySelector('[data-chat-scrollport]').scrollWidth
                        - document.querySelector('[data-chat-scrollport]').clientWidth};
            }"""
        )
        assert geometry["radius"] == "0px"
        assert geometry["background"] == "rgba(0, 0, 0, 0)"
        assert geometry["border"] == "0px"
        assert geometry["shadow"] == geometry["blur"] == "none"
        assert not geometry["physicalSurface"]
        assert all(
            abs(clock["right"] - geometry["referenceRight"]) <= 1
            for clock in geometry["clocks"]
        )
        assert geometry["overflow"] == geometry["listOverflow"] == 0
        assert abs(geometry["clocks"][0]["right"] - geometry["clocks"][1]["right"]) <= 1
        assert max(clock["topError"] for clock in geometry["clocks"]) <= 1
        assert all(clock["speakerSize"] == "17px" for clock in geometry["clocks"])
        assert all(clock["bodySize"] == "15px" for clock in geometry["clocks"])
        summary.press("Space")
        expect(disclosure).not_to_have_attribute("open", "")
        expect(disclosure.locator(".wechat-forwarded-body")).not_to_be_visible()
        assert not errors
        assert fixture.history.read_bytes() == fixture.preserved
        with sqlite3.connect(fixture.history) as connection:
            body, provenance = connection.execute(
                "SELECT text,provenance FROM messages WHERE id=?", (fixture.parent_id,)
            ).fetchone()
        assert body == "Forwarded conversation"
        assert all(text not in json.dumps(json.loads(provenance)) for text in ORIGINALS)
    finally:
        context.close()


def test_forwarded_details_remain_keyboard_operable_without_javascript(
    disposable_browser, forwarded_server
):
    fixture = forwarded_server
    context = disposable_browser.new_context(java_script_enabled=False)
    page = context.new_page()
    try:
        page.goto(
            fixture.url
            + "/browser?"
            + urlencode(
                {"view": "text", "source": "wechat", "session": fixture.session}
            ),
            wait_until="domcontentloaded",
        )
        reload_browser_after_verification(page)
        disclosure = page.locator(
            "#wechat_message_" + fixture.parent_id + " > details.wechat-forwarded"
        )
        expect(disclosure.locator(".wechat-forwarded-body")).not_to_be_visible()
        disclosure.locator(":scope > summary").press("Enter")
        expect(disclosure.locator(".wechat-forwarded-body")).to_be_visible()
        assert fixture.history.read_bytes() == fixture.preserved
    finally:
        context.close()
