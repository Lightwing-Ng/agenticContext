"""Account and conversation-type filters share desktop and narrow UI contracts.

Code version: v1.3.2-codex.0
"""

from __future__ import annotations

from io import BytesIO
import math
import re
from threading import Thread
from unittest.mock import patch

import pytest
from PIL import Image, ImageChops
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.config import CrawlConfig
from app.core.wechat_archive import import_wechat_archive
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_browser_chat_effects_e2e import (
    _assert_metrics_unaffected_by_chat_effects,
    _chat_geometry,
    _set_effect_layer_visibility,
    _wait_for_effect_sync,
)
from tests.test_wechat_archive import make_archive


disposable_browser = test_sidebar_e2e.disposable_browser


def _assert_directional_shadow_pixels(page, geometry, direction):
    """Prove painted shadow survives beyond the directional card's clipping edge."""
    boundary = geometry["list"]
    cards = [
        card
        for card in geometry["cards"]
        if card["visible"]
        and page.locator(f"#{card['id']}").get_attribute("data-wechat-direction")
        == direction
    ]
    target = max(
        cards,
        key=lambda card: (
            min(card["box"]["bottom"], boundary["bottom"])
            - max(card["box"]["top"], boundary["top"])
        ),
    )
    top = max(target["box"]["top"], boundary["top"]) + 12
    bottom = min(target["box"]["bottom"], boundary["bottom"]) - 12
    assert bottom - top >= 12
    center = (top + bottom) / 2
    left = boundary["right"] + 2 if direction == "sent" else boundary["left"] - 10
    crop = (
        math.floor(left),
        math.floor(center - 6),
        math.ceil(left + 8),
        math.ceil(center + 6),
    )
    painted = Image.open(BytesIO(page.screenshot())).convert("RGB")
    _set_effect_layer_visibility(page, "hidden")
    try:
        hidden = Image.open(BytesIO(page.screenshot())).convert("RGB")
    finally:
        _set_effect_layer_visibility(page, "")
    difference = ImageChops.difference(painted.crop(crop), hidden.crop(crop))
    assert (
        sum(
            max(difference.getpixel((x, y))) >= 1
            for x in range(difference.width)
            for y in range(difference.height)
        )
        >= 24
    )


def _assert_wechat_effect_geometry(page):
    _wait_for_effect_sync(page)
    geometry = _chat_geometry(page)
    assert geometry["padding"] == ["8px"] * 4
    assert geometry["listOverflow"] == ["hidden", "auto"]
    assert geometry["effectLayerOutsideList"] and geometry["effectLayerParent"]
    assert geometry["effectLayerHidden"] == "true"
    assert geometry["effectLayerPointerEvents"] == "none"
    assert geometry["effectLayerFocusable"] == 0
    assert geometry["trackMotion"] == [
        {"scrollTimeline": True, "sourceIsList": True, "axis": "block"}
    ]
    assert geometry["documentOverflow"] <= 1
    _assert_metrics_unaffected_by_chat_effects(page, geometry)
    return geometry


@pytest.fixture(scope="module")
def wechat_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-browser")
    archive = make_archive(sandbox / "archive", repeated=106)
    store = sandbox / "store"
    import_wechat_archive(
        archive, store, source_kinds={"xlsx-2": "group", "xlsx-3": "group"}
    )
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
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_filters_pagination_and_human_message_variants(
    disposable_browser, wechat_server, width, color_scheme
):
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
            wechat_server + "/browser?view=text&source=wechat", wait_until="networkidle"
        )
        if (
            width < 600
            and page.locator("#sidebar_toggle").get_attribute("aria-expanded") != "true"
        ):
            page.locator("#sidebar_toggle").click()
            expect(page.locator("#sidebar_toggle")).to_have_attribute(
                "aria-expanded", "true"
            )
        page.get_by_role(
            "button", name=re.compile(r"^Filter by WeChat account:")
        ).click()
        page.get_by_role("option", name="wxid_account_a · ount_a", exact=True).click()
        expect(page).to_have_url(
            re.compile(r"account=(?:account%3A|account:)wxid_account_a")
        )
        if (
            width < 600
            and page.locator("#sidebar_toggle").get_attribute("aria-expanded") != "true"
        ):
            page.locator("#sidebar_toggle").click()
            expect(page.locator("#sidebar_toggle")).to_have_attribute(
                "aria-expanded", "true"
            )
        page.get_by_role(
            "button", name=re.compile(r"^Filter by conversation type:")
        ).click()
        page.get_by_role("option", name="Group chats", exact=True).click()
        expect(page.locator("tr[data-chat-session-id]")).to_have_count(1)
        expect(page.locator("tr[data-chat-session-id]")).to_have_attribute(
            "data-wechat-conversation-kind", "group"
        )
        if (
            width < 600
            and page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "true"
        ):
            page.locator("#sidebar_toggle").click()
            expect(page.locator("#sidebar_toggle")).to_have_attribute(
                "aria-expanded", "false"
            )
        page.get_by_role("link", name="Same title", exact=True).click()
        expect(page.locator(".wechat-chat--group")).to_be_visible()
        expect(page.locator(".wechat-message")).to_have_count(100)
        assert page.locator(".wechat-message--sent").count() == 50
        assert page.locator(".wechat-message--received").count() == 50
        assert page.locator("[data-prompt-add]").count() == 0
        assert page.locator('img[src^="https://invalid.example"]').count() == 0
        geometry = _assert_wechat_effect_geometry(page)
        _assert_directional_shadow_pixels(page, geometry, "received")
        _assert_directional_shadow_pixels(page, geometry, "sent")
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        page.screenshot(path=f"test-results/wechat-{width}-{color_scheme}.png")
        page.get_by_role("link", name="Message page 2", exact=True).click()
        expect(page.locator(".wechat-message")).to_have_count(6)
        assert (
            re.search(r"account=(?:account%3A|account:)wxid_account_a", page.url)
            and "chat_kind=group" in page.url
        )
        page.get_by_role("link", name="All conversations", exact=True).click()
        expect(page.locator("tr[data-chat-session-id]")).to_have_count(1)
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("width", [1050, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_partial_message_shadow_does_not_draw_blank_body_below_scrollport(
    disposable_browser, wechat_server, width, color_scheme
):
    """Clipped messages cannot cast a false empty bubble into the pagination rail."""
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 778},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    page = context.new_page()
    try:
        page.goto(
            wechat_server
            + "/browser?view=text&source=wechat&account=account:wxid_account_a&chat_kind=group",
            wait_until="networkidle",
        )
        page.get_by_role("link", name="Same title", exact=True).click()
        if page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "true":
            page.locator("#sidebar_toggle").click()
        page.evaluate("document.fonts.ready")
        scrollport = page.locator("[data-chat-scrollport]")
        target = page.locator(".wechat-message--sent").nth(20)

        def below_scrollport_difference():
            _wait_for_effect_sync(page)
            _set_effect_layer_visibility(page, "")
            bounds = scrollport.bounding_box()
            crop = (
                max(0, math.floor(bounds["x"] - 12)),
                math.ceil(bounds["y"] + bounds["height"] + 2),
                min(width, math.ceil(bounds["x"] + bounds["width"] + 12)),
                math.floor(bounds["y"] + bounds["height"] + 32),
            )
            painted = Image.open(BytesIO(page.screenshot())).convert("RGB")
            _set_effect_layer_visibility(page, "hidden")
            try:
                hidden = Image.open(BytesIO(page.screenshot())).convert("RGB")
            finally:
                _set_effect_layer_visibility(page, "")
            return ImageChops.difference(painted.crop(crop), hidden.crop(crop))

        for position in ("middle", "end", "near-end", "return"):
            if position == "end":
                scrollport.evaluate("list => { list.scrollTop = list.scrollHeight; }")
            elif position == "near-end":
                scrollport.evaluate("list => { list.scrollTop -= 1; }")
            else:
                target.evaluate("""card => {
                    const list = card.closest('[data-chat-scrollport]');
                    list.scrollTop += card.getBoundingClientRect().top
                        - (list.getBoundingClientRect().bottom - 32);
                }""")
                assert target.locator(".wechat-message-text").evaluate("""text =>
                    text.getBoundingClientRect().top
                        >= text.closest('[data-chat-scrollport]').getBoundingClientRect().bottom
                """)
            difference = below_scrollport_difference()
            page.screenshot(
                path=f"/tmp/wechat-bottom-{width}-{color_scheme}-{position}.png"
            )
            if position == "end":
                assert (
                    sum(
                        max(difference.getpixel((x, y))) >= 1
                        for x in range(difference.width)
                        for y in range(difference.height)
                    )
                    >= 24
                )
            else:
                assert difference.getbbox() is None, (
                    position,
                    difference.getbbox(),
                )

        page.set_viewport_size({"width": width, "height": 1500})
        page.get_by_role("link", name="Message page 2", exact=True).click()
        expect(page.locator(".wechat-message")).to_have_count(6)
        _wait_for_effect_sync(page)
        assert scrollport.evaluate("list => list.scrollHeight <= list.clientHeight")
        _assert_directional_shadow_pixels(page, _chat_geometry(page), "sent")
        page.set_viewport_size({"width": width, "height": 778})
        page.wait_for_function("""() => {
            const list = document.querySelector('[data-chat-scrollport]');
            return innerHeight === 778 && list.scrollHeight > list.clientHeight;
        }""")
        assert below_scrollport_difference().getbbox() is None
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 812, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_exported_group_messages_align_left_and_reuse_name_metric(
    disposable_browser, wechat_server, width, color_scheme
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    page = context.new_page()
    try:
        page.goto(
            wechat_server
            + "/browser?view=text&source=wechat&account=source:xlsx-2&chat_kind=group",
            wait_until="networkidle",
        )
        page.get_by_role(
            "link", name="Group Name is not type evidence", exact=True
        ).click()
        # Wrapped metadata and normal line heights can leave fractional scrollport sizes.
        page.add_style_tag(
            content=".wechat-resources .browser-chat-pane { max-height: 301.5px; }"
        )
        expect(page.locator(".wechat-message--unknown")).to_have_count(100)
        expect(
            page.get_by_text("Direction unavailable in this export", exact=True)
        ).to_have_count(0)
        disclosure = page.locator(".wechat-provenance").first
        toggle = disclosure.locator("summary")
        expect(toggle).to_have_accessible_name("Source details")
        expect(toggle.locator(".wechat-message-header")).to_be_visible()
        expect(toggle.locator(".wechat-speaker")).to_have_text("Same display name")
        assert toggle.bounding_box()["height"] >= 32
        toggle.press("Enter")
        expect(disclosure).to_have_attribute("open", "")
        expect(
            disclosure.get_by_text("Unavailable in source export", exact=True)
        ).to_be_visible()
        toggle.press("Space")
        expect(disclosure).not_to_have_attribute("open", "")
        expect(disclosure.locator(".ui-collapse-body")).not_to_be_visible()
        compact = page.locator(".wechat-message").first.evaluate("""card => {
            const text = card.querySelector('.wechat-message-text').getBoundingClientRect();
            const summary = card.querySelector('summary').getBoundingClientRect();
            const box = card.getBoundingClientRect();
            const style = getComputedStyle(card);
            return {
                bodyAfterHeader: text.top >= summary.bottom,
                emptyFooter: box.bottom - text.bottom - parseFloat(style.paddingBottom)
                    - parseFloat(style.borderBottomWidth),
                nextCardGap: card.closest('.wechat-message-row').nextElementSibling
                    .querySelector('.wechat-message').getBoundingClientRect().top - box.bottom,
            };
        }""")
        assert compact["bodyAfterHeader"]
        assert abs(compact["emptyFooter"]) <= 1
        assert compact["nextCardGap"] == pytest.approx(12, abs=1)
        metric = page.locator(".wechat-conversation-name")
        expect(metric.locator(".metric-label")).to_have_text("Group chat")
        expect(metric.locator("strong")).to_have_text("Group Name is not type evidence")
        geometry = page.evaluate("""() => {
            const list = document.querySelector('.wechat-message-list');
            const message = list.querySelector('.wechat-message--unknown');
            const style = getComputedStyle(list);
            const expectedLeft = list.getBoundingClientRect().left
                + parseFloat(style.paddingLeft) + parseFloat(style.borderLeftWidth);
            const name = document.querySelector('.wechat-conversation-name strong');
            const reference = document.querySelector('.browser-metric-grid .metric-card strong');
            const roles = ['fontSize', 'fontWeight', 'lineHeight', 'backgroundImage', 'color'];
            return {
                leftOffset: Math.abs(message.getBoundingClientRect().left - expectedLeft),
                sameMetricStyle: roles.every(role => getComputedStyle(name)[role] === getComputedStyle(reference)[role]),
                fits: document.documentElement.scrollWidth <= innerWidth,
            };
        }""")
        assert geometry["leftOffset"] <= 1
        assert geometry["sameMetricStyle"]
        assert geometry["fits"]
        scrollport = page.locator("[data-chat-scrollport]")
        offsets = None
        for fraction in (0, 0.5, 1, 0.25):
            scrollport.evaluate(
                "(list, fraction) => { list.scrollTop = fraction * (list.scrollHeight - list.clientHeight); }",
                fraction,
            )
            effects = _assert_wechat_effect_geometry(page)
            actual = [card["effectOffset"] for card in effects["cards"]]
            assert offsets in (None, actual)
            offsets = actual
            _assert_directional_shadow_pixels(page, effects, "unknown")
        page.locator(".wechat-provenance summary").nth(25).click()
        expanded = _assert_wechat_effect_geometry(page)
        _assert_directional_shadow_pixels(page, expanded, "unknown")
        page.locator("[data-browser-chat-pane]").evaluate("""pane => {
            pane.style.maxHeight = '287.5px';
            pane.style.transform = 'scale(0.9)';
            pane.style.transformOrigin = 'top left';
        }""")
        resized = _assert_wechat_effect_geometry(page)
        _assert_directional_shadow_pixels(page, resized, "unknown")
        page.screenshot(
            path=f"test-results/wechat-export-layout-{width}-{color_scheme}.png"
        )
    finally:
        context.close()
