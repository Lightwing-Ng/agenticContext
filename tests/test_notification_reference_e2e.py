"""Production waiting-notice and catalog reference parity.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest
from playwright.sync_api import Browser, Locator, Page, Route, expect

from tests import test_sidebar_e2e


disposable_browser = test_sidebar_e2e.disposable_browser
sidebar_server_url = test_sidebar_e2e.sidebar_server_url

FIXTURE_MEDIA_ID = "notification-reference-fixture"
FIXTURE_MEDIA = {
    "id": FIXTURE_MEDIA_ID,
    "source": "chatgpt",
    "title": "Notification reference fixture",
    "filename": "notification-reference-fixture.png",
    "is_deleted": False,
}
FIXTURE_CARD = f"""
<article class="browser-media-card workspace-article-card"
         data-media-id="{FIXTURE_MEDIA_ID}" data-deleted="false">
    <button type="button" class="browser-media-open" data-media-open
            aria-label="Open notification reference fixture">
        <span class="browser-media-card-body">
            <span class="browser-media-card-title">Notification reference fixture</span>
        </span>
    </button>
    <button type="button" class="browser-media-remove dismiss-button"
            data-media-delete aria-label="Delete locally and stop tracking notification reference fixture"
            title="Delete locally and stop tracking">
        <span class="browser-media-remove-label">Delete locally and stop tracking</span>
    </button>
    <div class="browser-media-deleted-bar" data-media-deleted-bar hidden>
        <span data-media-deleted-message>Deleted locally; future tracking stopped</span>
        <button type="button" class="browser-media-undo" data-media-restore>Undo</button>
    </div>
</article>
"""
WRAPPED_TITLE = (
    "Completing the local request while keeping all unrelated resources unchanged"
)
WRAPPED_COPY = (
    "This deliberately long reference message verifies that a continuation line "
    "uses the same text column as its first line, while the topic icon retains "
    "the shared top alignment and the notice remains inside the viewport."
)


def _inject_isolated_media_document(route: Route) -> None:
    """Add one browser-only fixture before the real production listeners bind."""
    response = route.fetch()
    document = response.text()
    gallery_marker = 'aria-label="Cached media gallery">'
    assert document.count(gallery_marker) == 1
    document = document.replace(gallery_marker, gallery_marker + FIXTURE_CARD, 1)
    document, replacements = re.subn(
        r'(<script type="application/json" id="browser_media_data">).*?(</script>)',
        lambda match: match[1] + json.dumps([FIXTURE_MEDIA]) + match[2],
        document,
        count=1,
        flags=re.DOTALL,
    )
    assert replacements == 1
    route.fulfill(response=response, body=document)


def _notice_snapshot(surface: Locator) -> dict:
    return surface.evaluate("""surface => {
        const style = getComputedStyle(surface);
        const title = surface.querySelector('.workspace-modal-title, .notice-floating-banner-heading');
        const body = surface.querySelector('.workspace-modal-copy, .notice-floating-banner-list, .notice-floating-banner-copy');
        const icon = surface.querySelector('.workspace-modal-icon');
        const close = surface.querySelector('.workspace-modal-close, .notice-close');
        const glyph = close.querySelector('span');
        const rect = node => {
            const box = node.getBoundingClientRect();
            return {left: box.left, top: box.top, right: box.right, bottom: box.bottom,
                width: box.width, height: box.height, centerY: box.top + box.height / 2};
        };
        const layoutRect = node => {
            const parent = node.offsetParent;
            const origin = parent.getBoundingClientRect();
            const left = origin.left + parent.clientLeft + node.offsetLeft;
            const top = origin.top + parent.clientTop + node.offsetTop;
            return {left, top, right: left + node.offsetWidth, bottom: top + node.offsetHeight,
                width: node.offsetWidth, height: node.offsetHeight, centerY: top + node.offsetHeight / 2};
        };
        const material = node => {
            const computed = getComputedStyle(node);
            return {background: computed.background, border: computed.border,
                shadow: computed.boxShadow, blur: computed.backdropFilter};
        };
        const lineLefts = node => {
            const range = document.createRange();
            range.selectNodeContents(node);
            return Array.from(range.getClientRects(), box => box.left);
        };
        const probe = document.createElement('div');
        probe.style.cssText = 'position:absolute;pointer-events:none;'
            + 'background:var(--frosted-glass-notice-background);'
            + 'border:var(--frosted-glass-notice-border);'
            + 'box-shadow:var(--frosted-glass-notice-shadow);'
            + 'backdrop-filter:var(--frosted-glass-notice-blur)';
        surface.append(probe);
        const canonical = material(probe);
        probe.remove();
        return {
            material: material(surface), canonical,
            radius: style.borderRadius, padding: style.padding,
            gap: [style.columnGap, style.rowGap],
            surface: rect(surface), title: rect(title), body: rect(body),
            icon: layoutRect(icon), close: rect(close), glyph: rect(glyph),
            titleSize: getComputedStyle(title).fontSize,
            titleWeight: getComputedStyle(title).fontWeight,
            bodySize: getComputedStyle(body).fontSize,
            bodyWeight: getComputedStyle(body).fontWeight,
            bodyLineLefts: lineLefts(body.querySelector('li') || body),
            titleLineLefts: lineLefts(title),
            listPosition: body.matches('ol, ul') ? getComputedStyle(body).listStylePosition : null,
            internalOverflow: surface.scrollWidth - surface.clientWidth,
            documentOverflow: document.documentElement.scrollWidth - innerWidth,
            extraLayer: getComputedStyle(surface, '::after').content,
            viewport: {width: innerWidth, height: innerHeight},
        };
    }""")


def _assert_notice_contract(snapshot: dict, *, centered: bool, wrapped: bool) -> None:
    assert snapshot["material"] == snapshot["canonical"], snapshot
    assert snapshot["material"]["blur"] == "saturate(1.6) blur(18px)", snapshot
    assert snapshot["radius"] == "10px", snapshot
    assert snapshot["padding"] == "12px", snapshot
    assert snapshot["gap"] == ["12px", "4px"], snapshot
    assert (snapshot["titleSize"], snapshot["titleWeight"]) == ("15px", "600"), snapshot
    assert (snapshot["bodySize"], snapshot["bodyWeight"]) == ("15px", "400"), snapshot
    assert (snapshot["close"]["width"], snapshot["close"]["height"]) == (24, 24), (
        snapshot
    )
    assert (snapshot["glyph"]["width"], snapshot["glyph"]["height"]) == (12, 12), (
        snapshot
    )
    assert (snapshot["icon"]["width"], snapshot["icon"]["height"]) == (36, 36), snapshot
    assert snapshot["extraLayer"] == "none", snapshot
    assert snapshot["internalOverflow"] <= 1, snapshot
    assert snapshot["documentOverflow"] <= 1, snapshot
    assert abs(snapshot["icon"]["top"] - snapshot["body"]["top"]) <= 1, snapshot
    surface = snapshot["surface"]
    close = snapshot["close"]
    assert (
        abs((close["left"] - surface["left"]) - (close["top"] - surface["top"])) <= 1
    ), snapshot
    assert abs(snapshot["title"]["left"] - snapshot["icon"]["right"] - 12) <= 1, (
        snapshot
    )
    if not snapshot["listPosition"]:
        assert abs(snapshot["body"]["left"] - snapshot["title"]["left"]) <= 1, snapshot
    else:
        assert snapshot["listPosition"] == "outside", snapshot
    if wrapped:
        assert len(snapshot["titleLineLefts"]) >= 2, snapshot
        assert len(snapshot["bodyLineLefts"]) >= 2, snapshot
        assert max(snapshot["bodyLineLefts"]) - min(snapshot["bodyLineLefts"]) <= 1, (
            snapshot
        )
    else:
        assert abs(snapshot["title"]["centerY"] - close["centerY"]) <= 1, snapshot
    if centered:
        viewport = snapshot["viewport"]
        assert (
            abs(surface["left"] + surface["width"] / 2 - viewport["width"] / 2) <= 1
        ), snapshot
        assert abs(surface["centerY"] - viewport["height"] / 2) <= 1, snapshot
        assert surface["left"] >= 0 and surface["right"] <= viewport["width"], snapshot
        assert surface["top"] >= 0 and surface["bottom"] <= viewport["height"], snapshot


def _assert_dismiss_access(page: Page, surface: Locator, *, touch: bool) -> None:
    close = surface.locator(".workspace-modal-close, .notice-close")
    assert close.get_attribute("aria-label")
    page.mouse.move(0, 0)
    close.evaluate("node => node.blur()")
    if touch:
        expect(close).to_have_css("opacity", "1")
        expect(close).to_have_css("pointer-events", "auto")
    else:
        expect(close).to_have_css("opacity", "0")
        expect(close).to_have_css("pointer-events", "none")
        surface.hover()
        expect(close).to_have_css("opacity", "1")
        expect(close).to_have_css("pointer-events", "auto")
        page.mouse.move(0, 0)
        close.focus()
        expect(close).to_have_css("opacity", "1")
        expect(close).to_have_css("pointer-events", "auto")
    expect(close).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    expect(close).to_have_css("box-shadow", "none")
    assert close.evaluate("""node => {
        const probe = document.createElement('span');
        probe.style.color = 'var(--theme-error)';
        node.append(probe);
        const equal = getComputedStyle(node).color === getComputedStyle(probe).color;
        probe.remove();
        return equal;
    }""")


@pytest.fixture(scope="module")
def agent_access_document() -> str:
    """Render the real gate template without configuring or validating credentials."""
    from flask import Flask, render_template

    template_root = Path(__file__).resolve().parents[1] / "app/web/templates"
    application = Flask(
        "notification-reference-access",
        template_folder=str(template_root),
    )
    with application.test_request_context("/agent"):
        return render_template(
            "agent_access_unlock.html",
            product_name="AgenticContext",
            version="notification-reference",
            error_message=(
                "This isolated form checks long validation feedback without "
                "submitting the local Agent password."
            ),
        )


def _access_snapshot(surface: Locator) -> dict:
    return surface.evaluate("""surface => {
        const material = node => {
            const computed = getComputedStyle(node);
            return {background: computed.background, border: computed.border,
                shadow: computed.boxShadow, blur: computed.backdropFilter};
        };
        const rect = node => {
            const box = node.getBoundingClientRect();
            return {left: box.left, top: box.top, right: box.right, bottom: box.bottom,
                width: box.width, height: box.height};
        };
        const probe = document.createElement('div');
        probe.style.cssText = 'position:absolute;pointer-events:none;'
            + 'background:var(--frosted-glass-notice-background);'
            + 'border:var(--frosted-glass-notice-border);'
            + 'box-shadow:var(--frosted-glass-notice-shadow);'
            + 'backdrop-filter:var(--frosted-glass-notice-blur)';
        surface.append(probe);
        const canonical = material(probe);
        probe.remove();
        const overlayProbe = document.createElement('div');
        overlayProbe.className = 'workspace-modal-overlay';
        document.body.append(overlayProbe);
        const overlayBackground = getComputedStyle(overlayProbe).background;
        overlayProbe.remove();
        const computed = getComputedStyle(surface);
        const form = surface.querySelector('.agent-access-form');
        const actions = surface.querySelector('.agent-access-actions');
        const title = surface.querySelector('.agent-access-title');
        const overlay = surface.closest('.workspace-modal-overlay');
        const controls = [title, surface.querySelector('.agent-access-copy'),
            surface.querySelector('.agent-access-field'),
            surface.querySelector('.agent-access-error'), actions];
        return {
            material: material(surface), canonical,
            overlayBackground, actualOverlayBackground: getComputedStyle(overlay).background,
            overlayBlur: getComputedStyle(overlay).backdropFilter,
            surface: rect(surface), controls: controls.map(rect),
            radius: computed.borderRadius, padding: computed.padding,
            columns: computed.gridTemplateColumns.split(' ').length,
            formDisplay: getComputedStyle(form).display,
            formGap: getComputedStyle(form).gap,
            formMargin: getComputedStyle(form).marginTop,
            actionJustification: getComputedStyle(actions).justifyContent,
            titleSize: getComputedStyle(title).fontSize,
            titleWeight: getComputedStyle(title).fontWeight,
            extraLayer: getComputedStyle(surface, '::after').content,
            internalOverflow: surface.scrollWidth - surface.clientWidth,
            documentOverflow: document.documentElement.scrollWidth - innerWidth,
            viewport: {width: innerWidth, height: innerHeight},
        };
    }""")


@pytest.mark.parametrize("scheme", ("light", "dark"))
@pytest.mark.parametrize(
    ("width", "height", "touch", "motion"),
    (
        (1_024, 863, False, "no-preference"),
        (390, 844, True, "reduce"),
        (830, 480, True, "reduce"),
    ),
)
def test_delete_wait_and_catalog_notices_share_the_approved_reference(
    disposable_browser: Browser,
    sidebar_server_url: str,
    scheme: str,
    width: int,
    height: int,
    touch: bool,
    motion: str,
) -> None:
    """Exercise the real Delete listener without sending its POST to the server."""
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme=scheme,
        has_touch=touch,
        is_mobile=touch,
        reduced_motion=motion,
    )
    page = context.new_page()
    page.add_init_script(
        f"window.localStorage.setItem('cachelikes:theme-mode', {json.dumps(scheme)})"
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    pending_deletes: list[Route] = []
    page.route(re.compile(r"/browser\?"), _inject_isolated_media_document)
    page.route(
        f"**/api/browser/media/{FIXTURE_MEDIA_ID}/delete",
        lambda route: pending_deletes.append(route),
    )
    try:
        page.goto(f"{sidebar_server_url}/browser?view=media&source=chatgpt")
        expect(page.locator("html")).to_have_attribute("data-theme-override", scheme)
        delete = page.get_by_role(
            "button",
            name="Delete locally and stop tracking notification reference fixture",
            exact=True,
        )
        delete.focus()
        delete.click()
        overlay = page.locator("#cache_wait_modal")
        modal = overlay.locator(".workspace-modal-dialog")
        expect(modal).to_be_visible()
        assert len(pending_deletes) == 1
        assert pending_deletes[0].request.method == "POST"
        expect(modal.locator(".workspace-modal-title")).to_have_text(
            "Deleting local cache entry"
        )
        expect(modal.locator(".workspace-modal-copy")).to_have_text(
            "Removing this resource from local storage and stopping future tracking for it."
        )
        reference = _notice_snapshot(modal)
        _assert_notice_contract(reference, centered=True, wrapped=False)
        _assert_dismiss_access(page, modal, touch=touch)

        page.evaluate(
            """({title, copy}) => {
            window.notificationReferenceWait = window.CacheWaitModal.show({title, copy});
        }""",
            {"title": WRAPPED_TITLE, "copy": WRAPPED_COPY},
        )
        _assert_notice_contract(_notice_snapshot(modal), centered=True, wrapped=True)
        page.evaluate("window.notificationReferenceWait.finish()")
        modal.locator(".workspace-modal-close").focus()
        page.keyboard.press("Escape")
        expect(overlay).to_be_hidden()
        pending_deletes.pop().fulfill(
            json={"item": {**FIXTURE_MEDIA, "is_deleted": True}}
        )
        expect(page.locator(f'[data-media-id="{FIXTURE_MEDIA_ID}"]')).to_have_attribute(
            "data-deleted", "true"
        )

        page.goto(f"{sidebar_server_url}/settings/style-tokens")
        expect(page.locator("html")).to_have_attribute("data-theme-override", scheme)
        surfaces = page.locator(".style-token-modal-demo")
        expect(surfaces).to_have_count(2)
        for surface in surfaces.all():
            surface.scroll_into_view_if_needed()
            snapshot = _notice_snapshot(surface)
            _assert_notice_contract(snapshot, centered=False, wrapped=False)
            assert snapshot["material"] == reference["material"], snapshot
            _assert_dismiss_access(page, surface, touch=touch)
            surface.evaluate(
                """(node, {title, copy}) => {
                node.querySelector('.workspace-modal-title, .notice-floating-banner-heading').textContent = title;
                const list = node.querySelector('.notice-floating-banner-list');
                if (list) {
                    list.replaceChildren(...Array.from({length: 2}, () => {
                        const item = document.createElement('li');
                        item.textContent = copy;
                        return item;
                    }));
                } else node.querySelector('.workspace-modal-copy').textContent = copy;
            }""",
                {"title": WRAPPED_TITLE, "copy": WRAPPED_COPY},
            )
            _assert_notice_contract(
                _notice_snapshot(surface), centered=False, wrapped=True
            )
        assert not errors, errors
    finally:
        for route in pending_deletes:
            route.abort()
        context.close()


@pytest.mark.parametrize("scheme", ("light", "dark"))
@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((1_024, 863, False), (390, 844, True), (830, 480, True)),
)
def test_agent_access_retains_its_form_adapter_and_uses_the_approved_material(
    disposable_browser: Browser,
    sidebar_server_url: str,
    agent_access_document: str,
    scheme: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    """Inspect the real gate markup without contacting an unlock endpoint."""
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme=scheme,
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.add_init_script(
        f"window.localStorage.setItem('cachelikes:theme-mode', {json.dumps(scheme)})"
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    unlock_requests = []

    def prevent_unlock(route: Route) -> None:
        unlock_requests.append(route.request.method)
        route.abort()

    fixture_url = f"{sidebar_server_url}/notification-reference/access"
    page.route(
        fixture_url,
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=agent_access_document,
        ),
    )
    page.route("**/agent/unlock", prevent_unlock)
    try:
        page.goto(f"{sidebar_server_url}/browser?view=media&source=chatgpt")
        page.evaluate("window.notificationReferenceWait = window.CacheWaitModal.show()")
        reference = _notice_snapshot(
            page.locator("#cache_wait_modal .workspace-modal-dialog")
        )["material"]
        page.evaluate("window.notificationReferenceWait.finish()")

        page.goto(fixture_url)
        expect(page.locator("html")).to_have_attribute("data-theme-override", scheme)
        surface = page.get_by_role("dialog", name="Unlock Agent", exact=True)
        expect(surface).to_be_visible()
        snapshot = _access_snapshot(surface)
        assert snapshot["material"] == snapshot["canonical"] == reference, snapshot
        assert snapshot["material"]["blur"] == "saturate(1.6) blur(18px)", snapshot
        assert snapshot["radius"] == "10px", snapshot
        assert snapshot["padding"] == ("20px" if width <= 420 else "24px"), snapshot
        assert snapshot["surface"]["width"] == (width - 24 if width <= 420 else 380), (
            snapshot
        )
        assert snapshot["columns"] == 1, snapshot
        assert snapshot["formDisplay"] == "grid", snapshot
        assert snapshot["formGap"] == "16px", snapshot
        assert snapshot["formMargin"] == "22px", snapshot
        assert snapshot["actionJustification"] == "flex-end", snapshot
        assert (snapshot["titleSize"], snapshot["titleWeight"]) == ("20px", "600"), (
            snapshot
        )
        assert snapshot["extraLayer"] == "none", snapshot
        assert snapshot["overlayBlur"] == "none", snapshot
        assert snapshot["actualOverlayBackground"] == snapshot["overlayBackground"], (
            snapshot
        )
        assert snapshot["internalOverflow"] <= 1, snapshot
        assert snapshot["documentOverflow"] <= 1, snapshot
        box = snapshot["surface"]
        assert box["left"] >= 0 and box["right"] <= width, snapshot
        assert box["top"] >= 0 and box["bottom"] <= height, snapshot
        assert abs(box["left"] + box["width"] / 2 - width / 2) <= 1, snapshot
        assert abs(box["top"] + box["height"] / 2 - height / 2) <= 1, snapshot
        for control in snapshot["controls"]:
            assert box["left"] <= control["left"] <= control["right"] <= box["right"], (
                snapshot
            )
            assert box["top"] <= control["top"] <= control["bottom"] <= box["bottom"], (
                snapshot
            )

        form = surface.locator("form")
        expect(form).to_have_attribute("action", "/agent/unlock")
        expect(form).to_have_attribute("method", "post")
        slots = surface.locator(".agent-access-slot")
        expect(slots).to_have_count(6)
        expect(surface.locator(".agent-access-field")).to_have_attribute(
            "aria-label", "Agent password"
        )
        password = surface.locator("#agent_access_password")
        expect(password).to_have_attribute("type", "password")
        expect(password).to_have_attribute("inputmode", "numeric")
        expect(password).to_have_attribute("pattern", "[0-9]{6}")
        expect(password).to_have_attribute("minlength", "6")
        expect(password).to_have_attribute("maxlength", "6")
        expect(password).to_be_focused()
        submit = page.get_by_role("button", name="Unlock", exact=True)
        expect(submit).to_be_disabled()
        password.fill("12ab34")
        expect(password).to_have_value("1234")
        expect(surface.locator(".agent-access-slot.is-filled")).to_have_count(4)
        expect(submit).to_be_disabled()
        password.fill("102938")
        expect(surface.locator(".agent-access-slot.is-filled")).to_have_count(6)
        expect(submit).to_be_enabled()
        password.press("Tab")
        expect(submit).to_be_focused()
        password.fill("")
        expect(submit).to_be_disabled()
        password.evaluate("node => node.blur()")
        _assert_dismiss_access(page, surface, touch=touch)
        expect(
            surface.get_by_role("link", name="Back to Local resources")
        ).to_have_attribute("href", "/")
        assert not unlock_requests, unlock_requests
        assert not errors, errors
    finally:
        context.close()
