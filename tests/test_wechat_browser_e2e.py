"""Account and conversation-type filters share desktop and narrow UI contracts.

Code version: v1.7.3-codex.0
"""

from __future__ import annotations

from base64 import b64decode
import csv
from io import BytesIO, StringIO
import json
import math
import re
import sqlite3
from threading import Thread
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import pytest
from PIL import Image, ImageChops
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.config import CrawlConfig
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
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
    changed_pixels = sum(
        max(difference.getpixel((x, y))) >= 1
        for x in range(difference.width)
        for y in range(difference.height)
    )
    if changed_pixels < 24:
        details = page.evaluate("""({id, crop}) => {
            const card = document.getElementById(id);
            const pane = card?.closest('[data-browser-chat-pane]');
            const list = pane?.querySelector('[data-chat-scrollport]');
            const layer = pane?.querySelector('[data-chat-effects]');
            if (!card || !pane || !list || !layer) return {missingStructure: true};
            const effect = [...layer.querySelectorAll('[data-chat-effect-for]')]
                .find(node => node.dataset.chatEffectFor === id);
            const rect = node => {
                if (!node) return null;
                const box = node.getBoundingClientRect();
                return {left: box.left, right: box.right, top: box.top,
                    bottom: box.bottom, width: box.width, height: box.height};
            };
            const style = node => {
                if (!node) return null;
                const value = getComputedStyle(node);
                return {display: value.display, visibility: value.visibility,
                    opacity: value.opacity, shadow: value.boxShadow,
                    clip: value.clipPath, transform: value.transform};
            };
            const hit = document.elementFromPoint((crop[0] + crop[2]) / 2, (crop[1] + crop[3]) / 2);
            return {phase: 'after-restored-visible-render-frames',
                viewport: [innerWidth, innerHeight], deviceScale: devicePixelRatio,
                cropHit: hit ? {tag: hit.tagName, id: hit.id, classes: hit.className} : null,
                fonts: document.fonts.status,
                documentState: document.readyState,
                sidebarExpanded: document.querySelector('#sidebar_toggle')?.getAttribute('aria-expanded'),
                list: rect(list), client: [list.clientWidth, list.clientHeight],
                scroll: [list.scrollWidth, list.scrollHeight, list.scrollTop],
                card: rect(card), effect: rect(effect), effectStyle: style(effect),
                layer: rect(layer), layerStyle: style(layer),
                trackStyle: style(layer.querySelector('.browser-chat-effect-track'))};
        }""", {"id": target["id"], "crop": crop})
        print("Directional shadow comparison: " + json.dumps({
            "direction": direction, "changedPixels": changed_pixels, "crop": crop,
            "imageSize": painted.size, "measuredList": boundary,
            "measuredCard": target["box"], "presented": details,
        }, separators=(",", ":")))
    assert changed_pixels >= 24


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


def _assert_conversation_kind_badges(page, selector, theme):
    """Keep compact opaque badges legible without restyling source provenance."""
    badges = page.locator(selector)
    assert badges.count() > 0
    badges.evaluate_all("""async badges => {
        const transitions = badges.flatMap(badge => badge.getAnimations())
            .filter(animation => animation instanceof CSSTransition
                && Number.isFinite(animation.effect.getComputedTiming().endTime));
        await Promise.all(transitions.map(animation => animation.finished));
    }""")
    metrics = badges.evaluate_all("""badges => badges.map(badge => {
        const style = getComputedStyle(badge);
        const bounds = badge.getBoundingClientRect();
        const region = badge.closest('td, .wechat-conversation-labels');
        const regionBounds = region.getBoundingClientRect();
        const source = badge.closest('tr, header').querySelector('.wechat-source-label');
        const sourceStyle = getComputedStyle(source);
        let opacity = 1;
        for (let parent = badge; parent; parent = parent.parentElement) {
            opacity *= parseFloat(getComputedStyle(parent).opacity);
        }
        return {
            foreground: style.color,
            background: style.backgroundColor,
            opacity,
            radius: style.borderRadius,
            padding: [style.paddingTop, style.paddingRight,
                style.paddingBottom, style.paddingLeft],
            fontSize: style.fontSize,
            sharedFontSize: getComputedStyle(document.documentElement)
                .getPropertyValue('--font-ui-xs').trim(),
            themeMuted: getComputedStyle(document.documentElement)
                .getPropertyValue('--theme-muted').trim(),
            themeBackground: getComputedStyle(document.documentElement)
                .getPropertyValue('--theme-background').trim(),
            fontWeight: style.fontWeight,
            insideRegion: bounds.left >= regionBounds.left - 1
                && bounds.right <= regionBounds.right + 1,
            sourceColor: sourceStyle.color,
            sourceBackground: sourceStyle.backgroundColor,
            sourceOpacity: sourceStyle.opacity,
        };
    })""")

    def opaque_rgb(value):
        if value.startswith("#"):
            assert len(value) == 7
            return [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        channels = [float(number) for number in re.findall(r"[\d.]+", value)]
        assert len(channels) == 3 or channels[3] == 1
        if value.startswith("color(srgb "):
            return channels[:3]
        assert value.startswith("rgb(") or value.startswith("rgba(")
        return [channel / 255 for channel in channels[:3]]

    def luminance(rgb):
        channels = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in rgb
        ]
        return sum(
            channel * weight
            for channel, weight in zip(channels, (0.2126, 0.7152, 0.0722))
        )

    for metric in metrics:
        foreground_rgb = opaque_rgb(metric["foreground"])
        background_rgb = opaque_rgb(metric["background"])
        muted_rgb = opaque_rgb(metric["themeMuted"])
        surface_rgb = opaque_rgb(metric["themeBackground"])
        foreground = luminance(foreground_rgb)
        background = luminance(background_rgb)
        contrast = (max(foreground, background) + 0.05) / (
            min(foreground, background) + 0.05
        )
        assert contrast >= 4.5
        assert metric["opacity"] == 1
        assert metric["radius"] == "2px"
        assert metric["padding"] == ["2px", "6px", "2px", "6px"]
        assert metric["fontSize"] == metric["sharedFontSize"]
        assert metric["fontWeight"] == "500"
        assert metric["insideRegion"]
        assert opaque_rgb(metric["sourceColor"]) == muted_rgb
        if theme == "light":
            expected_fill = [
                muted * 0.16 + surface * 0.84
                for muted, surface in zip(muted_rgb, surface_rgb)
            ]
            assert background > 0.7
            assert background_rgb == pytest.approx(expected_fill, abs=0.00001)
            assert foreground_rgb == muted_rgb
        else:
            assert theme == "dark"
            assert background_rgb == muted_rgb
            assert foreground_rgb == surface_rgb
        assert metric["sourceBackground"] == "rgba(0, 0, 0, 0)"
        assert metric["sourceOpacity"] == "1"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.fixture(scope="module")
def wechat_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-browser")
    archive = make_archive(sandbox / "archive", repeated=106)
    store = sandbox / "store"
    import_wechat_archive(archive, store, source_kinds={"xlsx-2": "group"})
    with sqlite3.connect(wechat_archive_path(store)) as connection:
        connection.execute(
            "UPDATE conversations SET peer_id=?, title=? "
            "WHERE account_key=? AND kind='direct'",
            ("gh_fixture_news", "Official fixture", "account:wxid_account_b"),
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
        expect(page.locator(".wechat-conversation-share")).to_have_count(0)
        badges = page.locator(".wechat-conversation-kind .wechat-kind-badge")
        expect(badges).to_have_count(6)
        assert sorted(badges.all_text_contents()) == sorted(
            [
                "Group chat",
                "Private chat",
                "Group chat",
                "Official account",
                "Group chat",
                "Unclassified chat",
            ]
        )
        _assert_conversation_kind_badges(
            page, ".wechat-conversation-kind .wechat-kind-badge", color_scheme
        )
        assert page.locator("html").get_attribute("data-theme-override") is None
        opposite_theme = "dark" if color_scheme == "light" else "light"
        for manual_theme in (opposite_theme, color_scheme):
            page.locator("#global_theme_toggle").click()
            expect(page.locator("html")).to_have_attribute(
                "data-theme-override", manual_theme
            )
            expect(page.locator("#global_theme_toggle")).to_have_attribute(
                "data-effective-theme", manual_theme
            )
            _assert_conversation_kind_badges(
                page, ".wechat-conversation-kind .wechat-kind-badge", manual_theme
            )
        if page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "true":
            page.locator("#sidebar_toggle").click()
        page.get_by_role("link", name="Official fixture", exact=True).click()
        expect(page.locator(".wechat-conversation-context")).to_have_attribute(
            "data-wechat-conversation-kind", "direct"
        )
        expect(
            page.locator(".wechat-conversation-labels .wechat-kind-badge")
        ).to_have_text("Official account")
        _assert_conversation_kind_badges(
            page, ".wechat-conversation-labels .wechat-kind-badge", color_scheme
        )
        page.get_by_role("link", name="All conversations", exact=True).click()
        if page.locator("#sidebar_toggle").get_attribute("aria-expanded") != "true":
            page.locator("#sidebar_toggle").click()
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
        page.wait_for_load_state("networkidle")
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
        share = page.get_by_role("link", name="Export conversation as CSV", exact=True)
        expect(share).to_be_visible()
        if width < 600:
            sidebar_toggle = page.locator("#sidebar_toggle")
            sidebar_toggle.click()
            expect(sidebar_toggle).to_have_attribute("aria-expanded", "true")
            expect(share).to_be_hidden()
            expect(page.locator("#global_theme_toggle")).to_be_visible()
            sidebar_toggle.click()
            expect(sidebar_toggle).to_have_attribute("aria-expanded", "false")
            expect(share).to_be_visible()
        expect(share).to_have_attribute("title", "Export conversation as CSV")
        expect(page.get_by_role("link", name="Export text", exact=True)).to_have_count(
            0
        )
        expect(page.locator(".wechat-conversation-actions a")).to_have_count(0)
        back = page.get_by_role("link", name="All conversations", exact=True)
        expect(
            page.locator(".browser-content-toolbar > .wechat-conversation-back")
        ).to_have_count(1)
        expect(back).to_have_attribute("title", "Back to all conversations")
        expect(back).to_have_text("")
        expect(back.locator(".icon")).to_have_attribute("aria-hidden", "true")
        back_geometry = back.evaluate("""element => {
            const box = element.getBoundingClientRect();
            const icon = element.querySelector('.icon');
            const iconBox = icon.getBoundingClientRect();
            return {
                size: [box.width, box.height],
                iconSize: [iconBox.width, iconBox.height],
                mask: getComputedStyle(icon).maskImage,
            };
        }""")
        assert back_geometry["size"] == [32, 32], back_geometry
        assert back_geometry["iconSize"] == [18, 18], back_geometry
        assert back_geometry["mask"].endswith('/static/images/arrow.left.svg")')
        back.focus()
        expect(back).to_be_focused()
        share_geometry = share.evaluate("""element => {
            const share = element.getBoundingClientRect();
            const icon = element.querySelector('.icon').getBoundingClientRect();
            const theme = document.querySelector('#global_theme_toggle').getBoundingClientRect();
            const title = document.querySelector('.browser-heading-copy').getBoundingClientRect();
            return {
                size: [share.width, share.height],
                iconSize: [icon.width, icon.height],
                centerDifference: Math.abs(share.top + share.height / 2 - theme.top - theme.height / 2),
                gap: theme.left - share.right,
                titleClearsActions: title.right <= share.left + 1,
            };
        }""")
        assert share_geometry["size"] == [32, 32], share_geometry
        assert share_geometry["iconSize"] == [18, 18], share_geometry
        assert share_geometry["centerDifference"] <= 1, share_geometry
        assert share_geometry["gap"] == pytest.approx(10, abs=1), share_geometry
        assert share_geometry["titleClearsActions"], share_geometry
        page.locator("#global_theme_toggle").focus()
        page.keyboard.press("Tab")
        expect(share).to_be_focused()
        expect(
            page.locator(".wechat-conversation-labels .wechat-kind-badge")
        ).to_have_text("Group chat")
        _assert_conversation_kind_badges(
            page, ".wechat-conversation-labels .wechat-kind-badge", color_scheme
        )
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
        expect(page.locator(".wechat-conversation-context strong")).to_have_count(0)
        expect(
            page.get_by_role("heading", name="Same title", exact=True)
        ).to_have_count(1)
        with page.expect_download() as download_info:
            page.get_by_role(
                "link", name="Export conversation as CSV", exact=True
            ).click()
        download = download_info.value
        assert download.suggested_filename.endswith(".csv")
        body = download.path().read_bytes()
        assert body.startswith(b"\xef\xbb\xbf")
        reader = csv.DictReader(StringIO(body.decode("utf-8-sig"), newline=""))
        assert reader.fieldnames == ["Time", "Speaker ID", "Statement"]
        rows = list(reader)
        assert len(rows) == 106
        assert {row["Time"] for row in rows} == {"2021-08-07 18:59:03 UTC+08:00"}
        assert {row["Speaker ID"] for row in rows} == {
            "Same display name (wxid_peer)",
            "Same display name (wxid_account_a)",
        }
        assert all("wxid_account_a group" in row["Statement"] for row in rows)
        assert (
            re.search(r"account=(?:account%3A|account:)wxid_account_a", page.url)
            and "chat_kind=group" in page.url
        )
        page.get_by_role("link", name="All conversations", exact=True).press("Enter")
        expect(page.locator("tr[data-chat-session-id]")).to_have_count(1)
        expect(page.locator(".wechat-conversation-share")).to_have_count(0)
        assert (
            re.search(r"account=(?:account%3A|account:)wxid_account_a", page.url)
            and "chat_kind=group" in page.url
            and "sort=newest" in page.url
            and "session=" not in page.url
        )
        assert errors == []
    finally:
        context.close()


def _open_conversation_back_fixture(
    disposable_browser, wechat_server, *, width=1280, motion="reduce", message_page=2
):
    """Keep a nondefault list page separate from the current message page."""
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 937}, reduced_motion=motion
    )
    page = context.new_page()
    page.goto(
        wechat_server
        + "/browser?view=text&source=wechat&account=account:wxid_account_a"
        + "&chat_kind=group&sort=oldest&session_view=1",
        wait_until="networkidle",
    )
    session = page.get_by_role("link", name="Same title", exact=True)
    destination = urlsplit(urljoin(wechat_server, session.get_attribute("href")))
    values = parse_qs(destination.query)
    values.update(session_page=["7"], page=[str(message_page)])
    page.goto(
        destination._replace(query=urlencode(values, doseq=True)).geturl(),
        wait_until="networkidle",
    )
    if page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "true":
        page.locator("#sidebar_toggle").click()
    expect(page.locator(".wechat-message")).to_have_count(100 if message_page == 1 else 6)
    return context, page


def _install_conversation_back_probe(page, restore_events):
    """Observe pending content without evaluating a provisional document context."""
    probes = []
    page.on(
        "console",
        lambda message: (
            probes.append(json.loads(message.text.removeprefix("wechat-back-pending:")))
            if message.text.startswith("wechat-back-pending:")
            else None
        ),
    )
    page.evaluate("""events => {
        const originalMessage = document.querySelector('.wechat-message');
        const originalScrollport = document.querySelector('[data-chat-scrollport]');
        const readRestored = () => ({
            messages: document.querySelectorAll('.wechat-message').length,
            skeleton: !!document.querySelector('[data-browser-navigation-skeleton]'),
            workspaceBusy: document.getElementById('browser_workspace').getAttribute('aria-busy'),
            documentBusy: document.documentElement.getAttribute('aria-busy'),
            sameMessage: document.querySelector('.wechat-message') === originalMessage,
            sameScrollport: document.querySelector('[data-chat-scrollport]') === originalScrollport,
            scrollTop: originalScrollport.scrollTop,
            checkpoint: window.history.state?.agenticContextWechatContactReturnV1 || null,
            navigating: document.body.classList.contains('is-page-navigating'),
            target: document.documentElement.hasAttribute('data-navigation-target'),
        });
        // The window observer runs after production document listeners and never cancels navigation.
        window.addEventListener('click', event => {
            if (!event.target.closest('a.wechat-conversation-back')) return;
            const skeleton = document.querySelector(
                '#browser_workspace > [data-browser-navigation-skeleton]');
            const style = skeleton && getComputedStyle(skeleton);
            const box = skeleton?.getBoundingClientRect();
            const line = skeleton?.querySelector('.navigation-skeleton-line');
            console.debug('wechat-back-pending:' + JSON.stringify({
                visible: !!box && box.width > 0 && box.height > 0
                    && style.display !== 'none' && style.visibility === 'visible'
                    && Number(style.opacity) > 0,
                box: box && {left: box.left, top: box.top, right: box.right, bottom: box.bottom},
                messages: document.querySelectorAll('.wechat-message').length,
                workspaceBusy: document.getElementById('browser_workspace').getAttribute('aria-busy'),
                documentBusy: document.documentElement.getAttribute('aria-busy'),
                target: document.documentElement.dataset.navigationTarget,
                waiting: !document.getElementById('cache_wait_modal').hidden,
                originalConnected: originalMessage.isConnected,
                overflow: document.documentElement.scrollWidth - innerWidth,
                animation: line && getComputedStyle(line, '::after').animationName,
            }));
        }, {passive: true});
        // A test-only key triggers lifecycle probes after the compositor frame is captured.
        window.addEventListener('keydown', event => {
            if (event.code !== 'F24') return;
            const restored = events.map(type => {
                window.dispatchEvent(new PageTransitionEvent(type, {persisted: true}));
                return {type, ...readRestored()};
            });
            console.debug('wechat-back-restored:' + JSON.stringify(restored));
        });
    }""", restore_events)
    return probes


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize(
    ("activation", "motion"), [("click", "no-preference"), ("enter", "reduce")]
)
def test_conversation_back_paints_shared_skeleton_before_delayed_navigation(
    disposable_browser, wechat_server, width, activation, motion
):
    """Paint pending feedback and recover the same conversation on history restore."""
    context, page = _open_conversation_back_fixture(
        disposable_browser, wechat_server, width=width, motion=motion
    )
    pending = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        back = page.get_by_role("link", name="All conversations", exact=True)
        destination = back.evaluate("link => link.href")
        expected_scope = {
            "view": ["text"],
            "source": ["wechat"],
            "account": ["account:wxid_account_a"],
            "chat_kind": ["group"],
            "sort": ["oldest"],
            "page": ["7"],
        }
        assert parse_qs(urlsplit(destination).query) == expected_scope
        probes = _install_conversation_back_probe(page, ["pageshow"])
        cdp = context.new_cdp_session(page)
        screenshot_options = {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": {"x": 0, "y": 0, "width": width, "height": 937, "scale": 1},
        }
        original_pixels = Image.open(
            BytesIO(b64decode(cdp.send("Page.captureScreenshot", screenshot_options)["data"]))
        ).convert("RGB")
        page.route(destination, lambda route: pending.append(route))
        with page.expect_request(destination, timeout=5_000):
            if activation == "enter":
                back.press("Enter", no_wait_after=True)
            else:
                back.click(no_wait_after=True)
        pending_pixels = Image.open(
            BytesIO(b64decode(cdp.send("Page.captureScreenshot", screenshot_options)["data"]))
        ).convert("RGB")
        assert len(pending) == 1
        assert pending[0].request.is_navigation_request()
        assert len(probes) == 1, probes
        state = probes[0]
        assert state["visible"] is True, state
        assert state["messages"] == 0, state
        assert state["workspaceBusy"] == state["documentBusy"] == "true", state
        assert state["target"] == "browser-text", state
        assert state["waiting"] is False, state
        assert state["originalConnected"] is False, state
        assert state["overflow"] <= 1, state
        assert state["animation"] == (
            "none" if motion == "reduce" else "browser-navigation-skeleton-sheen"
        ), state
        assert pending_pixels.size == original_pixels.size == (width, 937)
        box = state["box"]
        crop = (
            max(0, math.floor(box["left"])),
            max(0, math.floor(box["top"])),
            min(width, math.ceil(box["right"])),
            min(937, math.ceil(box["bottom"])),
        )
        painted_difference = ImageChops.difference(
            original_pixels.crop(crop), pending_pixels.crop(crop)
        )
        pixels = painted_difference.tobytes()
        assert sum(
            max(red, green, blue) >= 4
            for red, green, blue in zip(pixels[::3], pixels[1::3], pixels[2::3], strict=True)
        ) >= 1_000

        with page.expect_console_message(
            predicate=lambda message: message.text.startswith("wechat-back-restored:")
        ) as restored_info:
            cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "F24", "code": "F24"})
        restored = json.loads(restored_info.value.text.removeprefix("wechat-back-restored:"))
        assert len(restored) == 1
        assert restored[0] == {
            "type": "pageshow",
            "messages": 6,
            "skeleton": False,
            "workspaceBusy": None,
            "documentBusy": None,
            "sameMessage": True,
            "sameScrollport": True,
            "scrollTop": 0,
            "checkpoint": None,
            "navigating": False,
            "target": False,
        }

        pending.pop().continue_()
        page.wait_for_url(destination)
        page.wait_for_load_state("networkidle")
        expect(page.locator("tr[data-chat-session-id]")).to_have_count(1)
        assert page.url == destination
        assert parse_qs(urlsplit(page.url).query) == expected_scope
        expect(page.locator("[data-browser-navigation-skeleton]")).to_have_count(0)
        expect(page.locator("html")).not_to_have_attribute("aria-busy", "true")
        assert errors == []
    finally:
        for route in pending:
            route.abort()
        context.close()



@pytest.mark.parametrize("width", [1280, 390])
def test_conversation_back_restores_scroll_before_contact_return_checkpoint(
    disposable_browser, wechat_server, width
):
    """Capture message geometry before the contact-return pagehide listener reads it."""
    context, page = _open_conversation_back_fixture(
        disposable_browser, wechat_server, width=width, message_page=1
    )
    pending = []
    try:
        checkpoint = page.locator("[data-chat-scrollport]").evaluate("""async scrollport => {
            scrollport.scrollTop = Math.min(850, scrollport.scrollHeight - scrollport.clientHeight);
            await new Promise(requestAnimationFrame);
            await new Promise(requestAnimationFrame);
            const bounds = scrollport.getBoundingClientRect();
            const anchor = [...scrollport.querySelectorAll('[data-chat-message-id][id]')]
                .find(message => message.getBoundingClientRect().bottom
                    > bounds.top + scrollport.clientTop + 1);
            const saved = {
                version: 1, url: window.location.href,
                scrollTop: scrollport.scrollTop, scrollLeft: scrollport.scrollLeft,
                documentX: window.scrollX, documentY: window.scrollY,
                anchorId: anchor.id, anchorOffset: anchor.getBoundingClientRect().top - bounds.top,
                openDetails: [], openForwarded: [], focusedLinkId: '',
            };
            window.history.replaceState({
                ...window.history.state, agenticContextWechatContactReturnV1: saved,
            }, '');
            return saved;
        }""")
        assert checkpoint["scrollTop"] > 0
        assert checkpoint["anchorId"]
        back = page.get_by_role("link", name="All conversations", exact=True)
        destination = back.evaluate("link => link.href")
        probes = _install_conversation_back_probe(page, ["pagehide", "pageshow"])
        cdp = context.new_cdp_session(page)
        page.route(destination, lambda route: pending.append(route))
        with page.expect_request(destination, timeout=5_000):
            back.click(no_wait_after=True)
        screenshot = cdp.send("Page.captureScreenshot", {"format": "png"})["data"]
        assert b64decode(screenshot).startswith(b"\x89PNG\r\n\x1a\n")
        assert len(pending) == 1
        assert len(probes) == 1 and probes[0]["visible"], probes
        with page.expect_console_message(
            predicate=lambda message: message.text.startswith("wechat-back-restored:")
        ) as restored_info:
            cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "F24", "code": "F24"})
        restored = json.loads(restored_info.value.text.removeprefix("wechat-back-restored:"))
        assert [state["type"] for state in restored] == ["pagehide", "pageshow"]
        for state in restored:
            assert state["messages"] == 100, state
            assert state["sameMessage"] and state["sameScrollport"], state
            assert state["scrollTop"] == checkpoint["scrollTop"], state
            assert state["checkpoint"] == checkpoint, state
            assert state["skeleton"] is False, state
            assert state["workspaceBusy"] is state["documentBusy"] is None, state
            assert not state["navigating"] and not state["target"], state
        pending.pop().continue_()
        page.wait_for_url(destination)
        page.wait_for_load_state("networkidle")
        expect(page.locator("tr[data-chat-session-id]")).to_have_count(1)
    finally:
        for route in pending:
            route.abort()
        context.close()


def test_conversation_back_retains_native_modified_and_non_document_actions(
    disposable_browser, wechat_server
):
    """Leave new-tab, download, and external actions to the browser."""
    context, page = _open_conversation_back_fixture(disposable_browser, wechat_server)
    try:
        back = page.get_by_role("link", name="All conversations", exact=True)
        before = page.url
        for action in (
            {"options": {"ctrlKey": True}},
            {"options": {"metaKey": True}},
            {"options": {"shiftKey": True}},
            {"options": {"altKey": True}},
            {"options": {"button": 1}},
            {"attribute": "download", "value": "conversation.html"},
            {"attribute": "target", "value": "_blank"},
            {"attribute": "href", "value": "https://example.invalid/browser?view=text"},
        ):
            result = back.evaluate(
                """(link, action) => {
                    const original = action.attribute ? link.getAttribute(action.attribute) : null;
                    if (action.attribute) link.setAttribute(action.attribute, action.value);
                    let intercepted = null;
                    // Observe production handling before suppressing this synthetic action.
                    window.addEventListener('click', event => {
                        intercepted = event.defaultPrevented;
                        event.preventDefault();
                    }, {once: true});
                    link.querySelector('.icon').dispatchEvent(new MouseEvent('click', {
                        bubbles: true, cancelable: true, button: 0, ...action.options,
                    }));
                    if (action.attribute) {
                        if (original === null) link.removeAttribute(action.attribute);
                        else link.setAttribute(action.attribute, original);
                    }
                    return {intercepted,
                        skeleton: !!document.querySelector('[data-browser-navigation-skeleton]'),
                        busy: document.documentElement.getAttribute('aria-busy'),
                        waiting: !document.getElementById('cache_wait_modal').hidden};
                }""",
                action,
            )
            assert result == {
                "intercepted": False,
                "skeleton": False,
                "busy": None,
                "waiting": False,
            }, action
        assert page.url == before
        expect(page.locator(".wechat-message")).to_have_count(6)
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 1024, 901, 900, 858, 390])
def test_conversation_back_stays_before_search_within_desktop_toolbar(
    disposable_browser, wechat_server, width
):
    """Keep Back beside desktop search and reachable in the compact layout."""
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 937}, reduced_motion="reduce"
    )
    page = context.new_page()
    try:
        page.goto(
            wechat_server
            + "/browser?view=text&source=wechat&account=account:wxid_account_a"
            + "&chat_kind=group&sort=newest&session_view=1",
            wait_until="networkidle",
        )
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        page.get_by_role("link", name="Same title", exact=True).click()
        back = page.get_by_role("link", name="All conversations", exact=True)
        expect(
            page.locator(".browser-content-toolbar > .wechat-conversation-back")
        ).to_have_count(1)
        expect(page.locator(".wechat-conversation-actions")).to_have_count(0)
        page.evaluate("document.fonts.ready")
        for expanded in (False, True, False):
            desired = "true" if expanded else "false"
            if toggle.get_attribute("aria-expanded") != desired:
                toggle.click()
            expect(toggle).to_have_attribute("aria-expanded", desired)
            if expanded and width <= 900:
                expect(back).to_be_hidden()
                continue
            expect(back).to_be_visible()
            metrics = back.evaluate("""async element => {
                await new Promise(requestAnimationFrame);
                await new Promise(requestAnimationFrame);
                const box = element.getBoundingClientRect();
                const icon = element.querySelector('.icon').getBoundingClientRect();
                const toggle = document.querySelector('#sidebar_toggle').getBoundingClientRect();
                const search = document.querySelector('.browser-search-control').getBoundingClientRect();
                const toolbar = element.closest('.browser-content-toolbar').getBoundingClientRect();
                const actions = element.nextElementSibling.getBoundingClientRect();
                const center = {x: box.x + box.width / 2, y: box.y + box.height / 2};
                return {
                    size: [box.width, box.height],
                    iconSize: [icon.width, icon.height],
                    centerXDifference: Math.abs(center.x - toggle.x - toggle.width / 2),
                    centerYDifference: Math.abs(center.y - search.y - search.height / 2),
                    toolbarLeftDifference: Math.abs(box.left - toolbar.left),
                    actionsGap: actions.left - box.right,
                    centerHit: document.elementFromPoint(center.x, center.y)?.closest('a') === element,
                    inViewport: box.left >= 0 && box.top >= 0
                        && box.right <= innerWidth && box.bottom <= innerHeight,
                    documentOverflow: document.documentElement.scrollWidth - innerWidth,
                };
            }""")
            assert metrics["size"] == [32, 32], metrics
            assert metrics["iconSize"] == [18, 18], metrics
            if width > 900:
                assert metrics["toolbarLeftDifference"] <= 1, metrics
                assert metrics["actionsGap"] == pytest.approx(12, abs=1), metrics
            else:
                assert metrics["centerXDifference"] <= 1, metrics
            assert metrics["centerYDifference"] <= 1, metrics
            assert metrics["centerHit"], metrics
            assert metrics["inViewport"], metrics
            assert metrics["documentOverflow"] <= 1, metrics
            back.focus()
            expect(back).to_be_focused()
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
def test_exported_group_messages_align_left_with_compact_context(
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
        expect(
            page.locator(".wechat-conversation-labels .wechat-kind-badge")
        ).to_have_text("Group chat")
        expect(page.locator(".wechat-conversation-context strong")).to_have_count(0)
        expect(
            page.get_by_role(
                "heading", name="Group Name is not type evidence", exact=True
            )
        ).to_have_count(1)
        geometry = page.evaluate("""() => {
            const list = document.querySelector('.wechat-message-list');
            const message = list.querySelector('.wechat-message--unknown');
            const style = getComputedStyle(list);
            const expectedLeft = list.getBoundingClientRect().left
                + parseFloat(style.paddingLeft) + parseFloat(style.borderLeftWidth);
            return {
                leftOffset: Math.abs(message.getBoundingClientRect().left - expectedLeft),
                fits: document.documentElement.scrollWidth <= innerWidth,
            };
        }""")
        assert geometry["leftOffset"] <= 1
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
