"""Nested transcript cards preserve one outer clock rail and literal source text.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from flask import Flask
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.config import CrawlConfig
from app.core.wechat_archive import _identifier, wechat_archive_path
from app.core.wechat_contacts import WechatContactRepository
from app.core.wechat_forwarded import _child
from app.core.wechat_text import native_components_text
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from app.web.presentation import format_wechat_source_clock
from tests import test_sidebar_e2e
from tests import test_wechat_forwarded_contact_analysis as html_fixtures
from tests.test_wechat_reconcile import merge, record, source
from tests.test_wechat_html_forwarded import _macro_message


disposable_browser = test_sidebar_e2e.disposable_browser
forwarded_activity_store = html_fixtures.forwarded_activity_store
BODY = "Literal <img src=https://invalid.example/track> remains text.\n" + (
    "https://example.invalid/" + "segment/" * 30
)
UNKNOWN = "Original participant: this literal colon is not another transcript."
PREVIEW = (
    'Known speaker: <img src="https://invalid.example/track">\n'
    "Another speaker: <script>window.previewExecuted = true</script>\n"
    "Clock-like prose: 2021-08-07 18:51:03 remains literal text."
)


def _preview_macro_module(contact_link=None):
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "app/web/templates"),
        autoescape=select_autoescape(("html", "xml")),
    )
    environment.filters["wechat_source_clock"] = format_wechat_source_clock
    context = {
        "text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=())
    }
    if contact_link is not None:
        context["wechat_contact_analysis_url"] = contact_link
    return environment.get_template("_wechat_browser.html").make_module(context)


def _presentation_child(number, text, *, record=None):
    child = _child(
        stable_id=f"{number:064x}",
        ordinal=number,
        text=text,
        author=f"Indexed participant {number}",
        sender=f"wxid_indexed_{number}",
        direction="received",
        timestamp=f"2021-08-07T18:{number:02d}:03",
        label=f"7 Aug 2021 18:{number:02d}:03",
        basis="Source clock; timezone unknown",
        provenance={},
        account_id="wxid_viewer",
    )
    if record is not None:
        child.messaging.update(forwarded_record=record, display_text="")
    return child


def _presentation_records():
    originals = tuple(
        _presentation_child(index, f"Indexed original body {index}")
        for index in range(1, 5)
    )
    matched = {
        "title": "Chat history",
        "items": originals,
        "authority": "matching_indexed_excerpt",
        "presentation_label": "Matched excerpt",
    }
    preview = {
        "title": "Chat history",
        "items": (),
        "preview_text": PREVIEW,
        "authority": "retained_export_preview",
        "presentation_label": "Preview only",
    }
    parent = _macro_message(
        {
            "title": "Chat history",
            "authority": "source_verified_html_export_tree",
            "items": (
                _presentation_child(10, "", record=matched),
                _presentation_child(11, "", record=preview),
            ),
        }
    )
    return parent, matched, preview


def test_preview_only_macro_preserves_escaped_literal_text_without_fabricated_events():
    _, _, preview = _presentation_records()
    rendered = str(
        _preview_macro_module().forwarded_conversation(preview, "synthetic-preview")
    )
    assert "Chat history" in rendered and "Preview only" in rendered
    assert "&lt;img" in rendered and "&lt;script&gt;" in rendered
    assert "<img" not in rendered and "<script" not in rendered
    assert "data-wechat-embedded-row" not in rendered and "<time" not in rendered
    assert "Original chat history is unavailable." not in rendered
    assert rendered.count("Clock-like prose:") == 1


@pytest.mark.parametrize("persisted", [False, True, None])
def test_unpersisted_matched_sender_keeps_its_name_without_an_unavailable_contact_link(
    persisted,
):
    calls = []

    def link(contact, _key):
        calls.append(contact)
        return "/browser/wechat/contacts/verified-contact"

    parent = _macro_message(text="Retained indexed statement")
    contact = {"id": "verified-contact", "identity_status": "verified"}
    if persisted is not None:
        contact["registry_persisted"] = persisted
    parent.messaging["contact"] = contact
    rendered = str(_preview_macro_module(link).message_row(parent))
    assert "Outer forwarding person" in rendered
    assert ("data-wechat-contact-link" in rendered) is (persisted is not False)
    assert len(calls) == (0 if persisted is False else 1)


@pytest.fixture(scope="module")
def preview_presentation_server():
    app = Flask(
        __name__, static_folder=str(Path(__file__).parents[1] / "app/web/static")
    )
    parent, _, _ = _presentation_records()
    rendered = str(_preview_macro_module().message_row(parent))

    @app.get("/")
    def presentation():
        return (
            '<!doctype html><html lang="en-US"><head><meta name="viewport" '
            'content="width=device-width, initial-scale=1">'
            '<link rel="stylesheet" href="/static/style.css">'
            '<link rel="stylesheet" href="/static/browser-messaging.css">'
            '<script defer src="/static/browser-messaging.js"></script>'
            '</head><body><section class="wechat-resources" data-wechat-resources>'
            '<div class="browser-chat-list wechat-message-list" data-chat-scrollport '
            'style="height:100vh;padding:12px;overflow:auto">'
            + rendered
            + "</div></section></body></html>"
        )

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}", parent=parent.stable_id
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_matched_excerpt_uses_four_standard_rows_while_preview_stays_literal(
    disposable_browser, preview_presentation_server, width, color_scheme
):
    fixture = preview_presentation_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1100},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    try:
        page.goto(fixture.url, wait_until="domcontentloaded")
        page.evaluate("() => document.fonts.ready")
        root = page.locator("#wechat_message_" + fixture.parent)
        outer = root.locator(":scope > details[data-wechat-forwarded]")
        expect(outer).not_to_have_attribute("open", "")
        outer.locator(":scope > summary").press("Enter")
        matched = page.locator(
            "#wechat_message_"
            + fixture.parent
            + "_forwarded_1 > details[data-wechat-forwarded]"
        )
        preview = page.locator(
            "#wechat_message_"
            + fixture.parent
            + "_forwarded_2 > details[data-wechat-forwarded]"
        )
        for details, label in ((matched, "Matched excerpt"), (preview, "Preview only")):
            expect(details).not_to_have_attribute("open", "")
            expect(
                details.locator(":scope > summary .wechat-forwarded-status")
            ).to_have_text(label)
            details.locator(":scope > summary").press("Enter")
        preview_body = preview.locator(":scope > .ui-collapse-body")
        expect(preview_body.locator(".wechat-message-text")).to_have_text(PREVIEW)
        expect(
            preview_body.locator(
                ".wechat-message-row, time, img, script, iframe, audio, video"
            )
        ).to_have_count(0)
        expect(matched.locator("[data-wechat-embedded-row]")).to_have_count(4)
        expect(matched.locator(".wechat-message-text")).to_have_text(
            [f"Indexed original body {index}" for index in range(1, 5)]
        )
        geometry = root.evaluate("""async card => {
            await new Promise(requestAnimationFrame);
            await new Promise(requestAnimationFrame);
            const rail = card.parentElement.querySelector(':scope > time').getBoundingClientRect().right;
            const clocks = [...card.querySelectorAll('.wechat-message-row > time')];
            return {count: clocks.length,
                railErrors: clocks.map(time => Math.abs(time.getBoundingClientRect().right-rail)),
                overflow: document.documentElement.scrollWidth-innerWidth,
                executed: !!window.previewExecuted};
        }""")
        assert geometry["count"] == 6 and max(geometry["railErrors"]) <= 1, geometry
        assert geometry["overflow"] == 0 and not geometry["executed"]
        preview.locator(":scope > summary").press("Space")
        expect(preview_body).not_to_be_visible()
        preview.locator(":scope > summary").press("Enter")
        expect(preview_body.locator(".wechat-message-text")).to_have_text(PREVIEW)
        expect(preview_body.locator(".wechat-message-row, time")).to_have_count(0)
    finally:
        context.close()


def _native_tree(owner):
    leaf = {
        "kind": "forwarded_text",
        "text": BODY,
        "source_sender_id": owner,
        "source_sender_label": "Original participant",
        "source_create_time": "1628333463",
        "original_message_type": 1,
    }
    unknown = {
        **leaf,
        "text": UNKNOWN,
        "source_create_time": "0",
        "source_sender_id": "wxid_original_peer",
        "source_sender_label": "Participant without a clock",
    }

    def wrap(children, name, clock):
        return {
            "kind": "forwarded_group",
            "text": native_components_text(children),
            "text_components": children,
            "source_sender_id": owner,
            "source_sender_label": name,
            "source_create_time": clock,
        }

    inner = wrap([leaf, unknown], "Inner forwarder", "1628333523")
    outer = wrap([inner], "Outer forwarder", "1628333583")
    sibling = wrap([deepcopy(leaf)], "Unopened sibling", "1628333643")
    return [outer, sibling]


def _html_tree(fixture):
    child = html_fixtures._component
    leaf = child(fixture, 11, BODY, raw_timestamp="2021-08-07 18:51:03")
    unknown = child(
        fixture,
        12,
        UNKNOWN,
        native=html_fixtures.OTHER,
        raw_timestamp="Unknown original time",
        flags=["unrecognized_export_timestamp"],
    )

    def wrap(number, children, clock):
        return child(
            fixture,
            number,
            "Explicit source transcript container",
            text_kind="forwarded_text",
            raw_timestamp=clock,
            forwarded_components=children,
        )

    inner = wrap(10, [leaf, unknown], "2021-08-07 18:52:03")
    outer = wrap(9, [inner], "2021-08-07 18:53:03")
    sibling = wrap(
        20, [child(fixture, 21, "Unopened sibling's own words")], "2021-08-07 18:54:03"
    )
    return [outer, sibling]


@pytest.fixture(params=["native", "html"])
def nested_server(request, tmp_path, forwarded_activity_store):
    kind = request.param
    if kind == "native":
        store = tmp_path / "native-store"
        metadata = source()
        owner = metadata["source_account_id"]
        parts = _native_tree(owner)
        parent = record(
            metadata,
            "nested-parent",
            text=native_components_text(parts),
            sender_id=owner,
            sender_display_name="Top forwarding owner",
            direction="sent",
            message_type=49,
            text_extraction_kind="forwarded_text",
            appmsg_subtype=19,
            text_components=parts,
            server_id="9999",
        )
        merge(store, [metadata], [parent])
        history = wechat_archive_path(store)
        parent_id = _identifier(metadata["source_id"], "nested-parent")
        with sqlite3.connect(history) as connection:
            session = connection.execute(
                "SELECT conversation_id FROM messages WHERE id=?", (parent_id,)
            ).fetchone()[0]
    else:
        fixture = forwarded_activity_store
        html_fixtures._publish_tree(
            fixture,
            _html_tree(fixture),
            primary="Chat history",
            kind="rendered_app_text",
        )
        resolved = WechatContactRepository(
            fixture["store"]
        ).resolve_forwarded_components([fixture["request"]])[fixture["request"]["id"]]
        assert resolved["status"] == "source_proof_verified"
        store, history = fixture["store"], fixture["history"]
        parent_id = fixture["request"]["id"]
        owner, session = (
            fixture["request"]["account_id"],
            fixture["request"]["conversation_id"],
        )
    preserved = history.read_bytes()
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            store,
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
            url=f"http://127.0.0.1:{server.server_port}",
            session=session,
            parent=parent_id,
            owner=owner,
            history=history,
            preserved=preserved,
            kind=kind,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _open(page, fixture, *, fragment=""):
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
        )
        + fragment,
        wait_until="domcontentloaded",
    )
    page.evaluate("() => document.fonts.ready")
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
    return page.locator("#wechat_message_" + fixture.parent)


def _assert_clock_rail(page, fixture):
    page.wait_for_function("""() => [...document.querySelectorAll('.wechat-message-row')]
        .filter(row => !row.closest('details:not([open])'))
        .every(row => !row.querySelector(':scope > time')
            || row.style.getPropertyValue('--wechat-time-top'))""")
    measurements = page.locator("#wechat_message_" + fixture.parent).evaluate("""async card => {
        await new Promise(requestAnimationFrame);
        await new Promise(requestAnimationFrame);
        const root = card.parentElement;
        const reference = root.querySelector(':scope > time').getBoundingClientRect().right;
        const rows = [root, ...card.querySelectorAll('.wechat-message-row')]
            .filter(row => !row.closest('details:not([open])'));
        const clocks = rows.flatMap(row => {
            const time = row.querySelector(':scope > time');
            if (!time) return [];
            const bubble = row.querySelector(':scope > article');
            const slot = bubble.querySelector(':scope > details.wechat-provenance .wechat-time-slot');
            return [{right: time.getBoundingClientRect().right,
                topError: Math.abs(time.getBoundingClientRect().top - slot.getBoundingClientRect().top),
                speakerSize: getComputedStyle(bubble.querySelector('.wechat-speaker')).fontSize,
                ownTimes: row.querySelectorAll(':scope > time').length}];
        });
        const port = card.closest('[data-chat-scrollport]');
        return {reference, clocks, documentOverflow: document.documentElement.scrollWidth-innerWidth,
            listOverflow: port.scrollWidth-port.clientWidth,
            media: card.querySelectorAll('img, video, audio, iframe').length,
            copies: [...card.querySelectorAll('.wechat-message-text')]
                .filter(node => !node.closest('details:not([open])')
                    && node.textContent === 'Literal <img src=https://invalid.example/track> remains text.\\n'
                    + 'https://example.invalid/' + 'segment/'.repeat(30)).length};
    }""")
    assert len(measurements["clocks"]) == 5, measurements
    assert all(
        abs(item["right"] - measurements["reference"]) <= 1
        for item in measurements["clocks"]
    ), measurements
    assert max(item["topError"] for item in measurements["clocks"]) <= 1, measurements
    assert all(
        item["speakerSize"] == "17px" and item["ownTimes"] == 1
        for item in measurements["clocks"]
    ), measurements
    assert measurements["documentOverflow"] == measurements["listOverflow"] == 0
    assert measurements["media"] == 0 and measurements["copies"] == 1


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_three_forwarded_levels_reuse_standard_cards_and_outer_clock_rail(
    disposable_browser, nested_server, width, color_scheme
):
    fixture = nested_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1100},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    try:
        card = _open(page, fixture)
        current = card
        disclosures = []
        for depth in range(3):
            disclosure = current.locator(":scope > details[data-wechat-forwarded]")
            expect(disclosure).not_to_have_attribute("open", "")
            summary = disclosure.locator(":scope > summary")
            expect(summary).to_have_text("Chat history")
            summary.focus()
            summary.press("Enter")
            expect(disclosure).to_have_attribute("open", "")
            disclosures.append(disclosure)
            current = page.locator(
                "#wechat_message_" + fixture.parent + "_forwarded_1" * (depth + 1)
            )
        expect(current.locator(":scope > .wechat-message-text")).to_have_text(BODY)
        unknown = page.locator(
            "#wechat_message_" + fixture.parent + "_forwarded_1_forwarded_1_forwarded_2"
        )
        expect(unknown.locator(":scope > .wechat-message-text")).to_have_text(UNKNOWN)
        expect(unknown.locator("xpath=..").locator(":scope > time")).to_have_count(0)
        expect(
            page.locator(
                "#wechat_message_"
                + fixture.parent
                + "_forwarded_2 > details[data-wechat-forwarded]"
            )
        ).not_to_have_attribute("open", "")
        _assert_clock_rail(page, fixture)
        disclosures[1].locator(":scope > summary").press("Space")
        expect(current).not_to_be_visible()
        disclosures[1].locator(":scope > summary").press("Enter")
        expect(current).to_be_visible()
        page.set_viewport_size(
            {"width": 390 if width == 1280 else 1280, "height": 1100}
        )
        _assert_clock_rail(page, fixture)
        assert fixture.history.read_bytes() == fixture.preserved
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_deep_forwarded_fragment_reveals_only_its_three_ancestors(
    disposable_browser, nested_server, width
):
    fixture = nested_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1100}, reduced_motion="reduce"
    )
    page = context.new_page()
    key = fixture.parent + "_forwarded_1_forwarded_1_forwarded_1"
    try:
        card = _open(page, fixture, fragment="#wechat_message_" + key)
        target = page.locator("#wechat_message_" + key)
        expect(target).to_be_visible()
        expect(card.locator("details[data-wechat-forwarded][open]")).to_have_count(3)
        expect(card.locator("details.wechat-provenance[open]")).to_have_count(0)
        expect(
            page.locator(
                "#wechat_message_"
                + fixture.parent
                + "_forwarded_2 > details[data-wechat-forwarded]"
            )
        ).not_to_have_attribute("open", "")
        _assert_clock_rail(page, fixture)
        assert fixture.history.read_bytes() == fixture.preserved
    finally:
        context.close()
