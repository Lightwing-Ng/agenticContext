"""Mac text observations add quote context without rewriting native message rows.

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
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_wechat_text_observations import make_observation_fixture


disposable_browser = test_sidebar_e2e.disposable_browser
OBSERVATION_LABEL = (
    "Mac client text supplied by user; native reference IDs unavailable."
)


@pytest.fixture(scope="module")
def text_observation_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-text-observation-browser")
    store = sandbox / "store"
    evidence = make_observation_fixture(store)
    history = evidence["history_path"]
    preserved = history.read_bytes()
    with sqlite3.connect(history) as connection:
        primary_count = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=?",
            (evidence["session_id"],),
        ).fetchone()[0]
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
            **evidence,
            url=f"http://127.0.0.1:{server.server_port}",
            preserved=preserved,
            primary_count=primary_count,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def open_observed_quote(page, fixture):
    page.goto(
        fixture.url
        + "/browser?"
        + urlencode(
            {
                "view": "text",
                "source": "wechat",
                "account": "account:" + fixture.account_id,
                "session": fixture.session_id,
            }
        ),
        wait_until="domcontentloaded",
    )
    page.evaluate("() => document.fonts.ready")
    sidebar = page.locator("#sidebar_toggle")
    if sidebar.get_attribute("aria-expanded") == "true":
        sidebar.click()
    parent = page.locator("#wechat_message_" + fixture.message_id)
    expect(parent).to_have_count(1)
    return parent


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_mac_observation_projects_a_compact_forwarded_quote_without_native_writes(
    disposable_browser, text_observation_server, width, color_scheme
):
    fixture = text_observation_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        parent = open_observed_quote(page, fixture)
        expect(parent.locator(":scope > .wechat-message-text")).to_have_text(
            fixture.new_reply
        )
        disclosure = parent.locator(":scope > details.wechat-forwarded")
        expect(disclosure).to_have_count(1)
        expect(disclosure).not_to_have_attribute("open", "")
        expect(disclosure.locator(".wechat-forwarded-body")).not_to_be_visible()
        expect(
            parent.get_by_text("Original quoted content is unavailable.", exact=True)
        ).to_have_count(0)
        expect(page.locator("[data-chat-message-id]")).to_have_count(
            fixture.primary_count
        )

        source = parent.locator(":scope > details.wechat-provenance")
        label = source.get_by_text(OBSERVATION_LABEL, exact=True)
        expect(label).to_have_count(1)
        expect(label).not_to_be_visible()
        expect(source.get_by_text("Additional evidence", exact=True)).to_have_count(1)
        expect(disclosure.get_by_text(OBSERVATION_LABEL, exact=True)).to_have_count(0)

        summary = disclosure.locator(":scope > summary")
        expect(summary).to_have_text("Chat history")
        summary.click()
        expect(disclosure).to_have_attribute("open", "")
        children = disclosure.locator("[data-wechat-embedded-row]")
        expect(children).to_have_count(3)
        expect(children.nth(0).locator(".wechat-resource-label")).to_have_text("Image")
        expect(children.nth(1).locator(".wechat-message-text")).to_have_text(
            fixture.excerpt_text
        )
        expect(children.nth(2).locator(".wechat-message-text")).to_have_text(
            fixture.canonical_text
        )
        expect(disclosure.locator("img, iframe, video, audio")).to_have_count(0)
        expect(disclosure.locator("[data-wechat-resource-reveal]")).to_have_count(0)
        expect(children.get_by_text("Additional evidence", exact=True)).to_have_count(0)
        page.wait_for_function(
            """id => [...document.getElementById(id)
                .querySelectorAll('[data-wechat-embedded-row]')]
                .every(row => row.style.getPropertyValue('--wechat-time-top'))""",
            arg="wechat_message_" + fixture.message_id,
        )
        geometry = disclosure.evaluate(
            """details => {
                const body = details.querySelector('.wechat-forwarded-body');
                const clocks = [...details.querySelectorAll('[data-wechat-embedded-row]')]
                    .map(row => {
                        const bubble = row.querySelector(':scope > article');
                        const time = row.querySelector(':scope > time');
                        const slot = bubble.querySelector(':scope > details > summary .wechat-time-slot');
                        return {
                            right: time.getBoundingClientRect().right,
                            topError: Math.abs(time.getBoundingClientRect().top
                                - slot.getBoundingClientRect().top),
                            speakerSize: getComputedStyle(bubble.querySelector('.wechat-speaker')).fontSize,
                            bodySize: getComputedStyle(bubble.querySelector('.wechat-message-text, .wechat-resource-label')).fontSize,
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
                        - document.querySelector('[data-chat-scrollport]').clientWidth,
                    idsUnique: new Set([...document.querySelectorAll('[id]')]
                        .map(node => node.id)).size === document.querySelectorAll('[id]').length};
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
        assert geometry["idsUnique"]
        rights = [clock["right"] for clock in geometry["clocks"]]
        assert max(rights) - min(rights) <= 1
        assert max(clock["topError"] for clock in geometry["clocks"]) <= 1
        assert all(clock["speakerSize"] == "17px" for clock in geometry["clocks"])
        assert all(clock["bodySize"] == "15px" for clock in geometry["clocks"])

        source.locator(":scope > summary").click()
        expect(label).to_be_visible()
        source.locator(":scope > summary").click()
        expect(label).not_to_be_visible()
        summary.click()
        expect(disclosure).not_to_have_attribute("open", "")
        summary.press("Enter")
        expect(disclosure).to_have_attribute("open", "")
        summary.press("Space")
        expect(disclosure).not_to_have_attribute("open", "")
        expect(disclosure.locator(".wechat-forwarded-body")).not_to_be_visible()
        page.wait_for_function(
            """id => {
                const ids = new Set([...document.getElementById(id)
                    .querySelectorAll('.wechat-forwarded .browser-chat-message, [data-chat-physical-surface]')]
                    .map(node => node.id));
                return [...document.querySelectorAll('[data-chat-effect-for]')]
                    .every(effect => !ids.has(effect.dataset.chatEffectFor) || effect.hidden);
            }""",
            arg="wechat_message_" + fixture.message_id,
        )
        assert not errors
        assert fixture.history_path.read_bytes() == fixture.preserved
        with sqlite3.connect(fixture.history_path) as connection:
            primary = connection.execute(
                "SELECT text FROM messages WHERE id=?", (fixture.message_id,)
            ).fetchone()[0]
        assert primary == fixture.original_text
    finally:
        context.close()


@pytest.mark.parametrize("invalid_evidence", ["artifact_hash", "account_scope"])
def test_invalid_mac_observation_retains_the_original_unavailable_quote(
    disposable_browser, text_observation_server, invalid_evidence
):
    fixture = text_observation_server
    artifact = fixture.artifact_path.read_bytes()
    registry = fixture.registry_path.read_bytes()
    context = disposable_browser.new_context(reduced_motion="reduce")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        if invalid_evidence == "artifact_hash":
            fixture.artifact_path.write_bytes(artifact + b"\n")
        else:
            value = json.loads(registry)
            value["observations"][0]["account_id"] = "wxid_wrong_synthetic_account"
            fixture.registry_path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        parent = open_observed_quote(page, fixture)
        expect(parent.locator(":scope > .wechat-message-text")).to_have_text(
            fixture.original_text
        )
        expect(parent.locator(":scope > details.wechat-forwarded")).to_have_count(0)
        expect(
            parent.get_by_text("Original quoted content is unavailable.", exact=True)
        ).to_be_visible()
        expect(parent.get_by_text(OBSERVATION_LABEL, exact=True)).to_have_count(0)
        expect(parent.get_by_text("Additional evidence", exact=True)).to_have_count(0)
        assert not errors
        assert fixture.history_path.read_bytes() == fixture.preserved
    finally:
        fixture.artifact_path.write_bytes(artifact)
        fixture.registry_path.write_bytes(registry)
        context.close()
