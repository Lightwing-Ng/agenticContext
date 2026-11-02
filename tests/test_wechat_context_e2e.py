"""Quoted and nested forwards reuse one compact transcript across viewports.

Code version: v1.1.2-codex.0
"""

from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.chat_history_browser import query_chat_history
from app.core.config import CrawlConfig
from app.core.wechat_archive import _identifier, wechat_archive_path
from app.core.wechat_text import extract_wechat_text
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_wechat_reconcile import merge, record, source


disposable_browser = test_sidebar_e2e.disposable_browser


def _item(kind, body, *, name="Original participant", sender="wxid_original"):
    return (
        f'<dataitem datatype="{kind}"><sourcename>{name}</sourcename>'
        "<srcMsgCreateTime>1628333463</srcMsgCreateTime><dataitemsource>"
        f"<fromusr>{sender}</fromusr></dataitemsource>{body}</dataitem>"
    )


@pytest.fixture(scope="module")
def context_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-native-context")
    store = sandbox / "store"
    metadata = source()
    inner = (
        "<recordinfo><datalist>"
        + _item(
            1,
            "<datadesc>Nested original &lt;script&gt;literal&lt;/script&gt;</datadesc>",
        )
        + "</datalist></recordinfo>"
    )
    tree = (
        "<recordinfo><datalist>"
        + _item(2, "")
        + _item(1, "<datadesc>Ordinary forwarded text</datadesc>")
        + _item(17, "<recordinfo>" + escape(inner) + "</recordinfo>", name="Forwarder")
        + "</datalist></recordinfo>"
    )
    forward = (
        "<msg><appmsg><type>19</type><title>Forwarded conversation</title><recorditem>"
        + escape(tree)
        + "</recorditem></appmsg></msg>"
    )
    quoted = (
        "<msg><appmsg><type>57</type><title>Current literal reply</title><refermsg>"
        "<type>49</type><displayname>Different forwarding participant</displayname>"
        "<fromusr>wxid_forwarder</fromusr><content>"
        + escape(forward)
        + "</content></refermsg></appmsg></msg>"
    )
    records = []
    for index, envelope in enumerate((forward, quoted)):
        parsed = extract_wechat_text(49, envelope)
        assert parsed is not None
        records.append(
            record(
                metadata,
                f"context-{index}",
                message_type=49,
                server_id=str(8000 + index),
                **parsed,
            )
        )
    merge(store, [metadata], records)
    history = wechat_archive_path(store)
    preserved = history.read_bytes()
    sessions = query_chat_history(store, source="wechat").sessions
    assert len(sessions) == 1
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
            session=sessions[0].stable_id,
            parent_ids=[
                _identifier(metadata["source_id"], f"context-{i}") for i in range(2)
            ],
            history=history,
            preserved=preserved,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
@pytest.mark.parametrize("quoted", [False, True])
def test_mixed_nested_forward_and_quote_keep_structure_and_shared_geometry(
    disposable_browser, context_server, width, color_scheme, quoted
):
    fixture = context_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1100},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(
            fixture.url
            + "/browser?"
            + urlencode(
                {"view": "text", "source": "wechat", "session": fixture.session}
            ),
            wait_until="domcontentloaded",
        )
        # The shared face metrics must be ready before measuring nested transcript clocks.
        page.evaluate("() => document.fonts.ready")
        sidebar = page.locator("#sidebar_toggle")
        if sidebar.get_attribute("aria-expanded") == "true":
            sidebar.click()
        parent = page.locator("#wechat_message_" + fixture.parent_ids[int(quoted)])
        outer = parent.locator(":scope > details.wechat-forwarded")
        expect(outer).to_have_count(1)
        expect(outer).not_to_have_attribute("open", "")
        expect(outer.locator(":scope > summary")).to_have_text("Chat history")
        if quoted:
            expect(parent.locator(":scope > .wechat-message-text")).to_have_text(
                "Current literal reply"
            )
        outer.locator(":scope > summary").click()
        body = outer.locator(":scope > .wechat-forwarded-body")
        expect(body.locator(":scope > [data-wechat-transcript]")).to_have_attribute(
            "aria-label", "Chat history messages"
        )
        rows = body.locator(
            ":scope > [data-wechat-transcript] > [data-wechat-embedded-row]"
        )
        expect(rows).to_have_count(3)
        expect(rows.nth(0).locator(".wechat-resource-label")).to_have_text("Image")
        expect(rows.nth(1).locator(".wechat-message-text")).to_have_text(
            "Ordinary forwarded text"
        )
        nested = rows.nth(2).locator("details.wechat-forwarded")
        expect(nested).not_to_have_attribute("open", "")
        expect(nested.locator(":scope > summary")).to_have_text("Chat history")
        expect(nested.locator(".wechat-forwarded-body")).not_to_be_visible()
        nested.locator(":scope > summary").press("Enter")
        expect(nested).to_have_attribute("open", "")
        expect(nested.locator(".wechat-message-text")).to_have_text(
            "Nested original <script>literal</script>"
        )
        expect(
            outer.locator("img,video,audio,iframe,[data-wechat-resource-reveal]")
        ).to_have_count(0)
        page.wait_for_function(
            """id => {
                const root = document.getElementById(id);
                const rows = [...root.querySelectorAll('[data-wechat-embedded-row]')];
                if (!rows.length || !rows.every(row => row.style.getPropertyValue('--wechat-time-top'))) return false;
                const elements = [
                    root.closest('.wechat-message-row').querySelector(':scope > time'),
                    ...rows.flatMap(row => [row, row.querySelector(':scope > time'),
                        row.querySelector(':scope > article > details.wechat-provenance .wechat-time-slot')]),
                ];
                if (!elements.every(element => element && element.getClientRects().length)) return false;
                const key = elements.map(element => {
                    const rect = element.getBoundingClientRect();
                    return [rect.x, rect.y, rect.width, rect.height].map(value => value.toFixed(3)).join(',');
                }).join(';');
                const previous = window.__wechatContextStableRects;
                window.__wechatContextStableRects = {id, key};
                return previous?.id === id && previous.key === key;
            }""",
            arg="wechat_message_" + fixture.parent_ids[int(quoted)],
            polling="raf",
        )
        geometry = outer.evaluate(
            """details => {
                const mainClock = details.closest('.wechat-message-row')
                    .querySelector(':scope > time').getBoundingClientRect();
                const clocks = [...details.querySelectorAll('[data-wechat-embedded-row]')]
                    .map(row => {
                        const time = row.querySelector(':scope > time');
                        const slot = row.querySelector(':scope > article > details.wechat-provenance .wechat-time-slot');
                        return {right:time.getBoundingClientRect().right,
                            error:Math.abs(time.getBoundingClientRect().top - slot.getBoundingClientRect().top)};
                    });
                const bodies = [...details.querySelectorAll('.wechat-forwarded-body')].map(node => {
                    const style = getComputedStyle(node);
                    return {radius:style.borderRadius, background:style.backgroundColor,
                        border:parseFloat(style.borderTopWidth), shadow:style.boxShadow,
                        blur:style.backdropFilter || style.webkitBackdropFilter,
                        physicalSurface:node.hasAttribute('data-chat-physical-surface')};
                });
                return {clocks,bodies,mainClockRight:mainClock.right,
                    overflow:document.documentElement.scrollWidth - innerWidth,
                    idsUnique:new Set([...document.querySelectorAll('[id]')].map(n => n.id)).size === document.querySelectorAll('[id]').length};
            }"""
        )
        assert geometry["overflow"] == 0 and geometry["idsUnique"]
        assert geometry["bodies"]
        for body_style in geometry["bodies"]:
            assert body_style == {
                "radius": "0px",
                "background": "rgba(0, 0, 0, 0)",
                "border": 0,
                "shadow": "none",
                "blur": "none",
                "physicalSurface": False,
            }
        assert max(clock["error"] for clock in geometry["clocks"]) <= 1
        rights = [clock["right"] for clock in geometry["clocks"]]
        assert max(rights) - min(rights) <= 1
        assert max(abs(right - geometry["mainClockRight"]) for right in rights) <= 1
        nested.locator(":scope > summary").press("Space")
        expect(nested).not_to_have_attribute("open", "")
        outer.locator(":scope > summary").click()
        expect(outer).not_to_have_attribute("open", "")
        assert not errors
        assert fixture.history.read_bytes() == fixture.preserved
    finally:
        context.close()
