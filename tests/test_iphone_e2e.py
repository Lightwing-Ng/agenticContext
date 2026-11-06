"""Rendered iPhone mode navigation, setup gating, live preview, and click-to-tap.

Every ``/api/iphone/*`` request is fulfilled in the browser, so these checks never create
the iPhone runtime, start a process, or contact a device. The test server keeps external
Agent operations disabled; the mocked status endpoint is what enables the page.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from copy import deepcopy
from io import BytesIO
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image
from playwright.sync_api import Browser, expect, sync_playwright

from tests import test_sidebar_e2e as fixtures

sidebar_server_url = fixtures.sidebar_server_url

MODE_NAV = '[aria-label="Agent modes"]'
MODE_LABELS = ["Agentic", "Jurors", "iPhone"]
SETUP_HEADINGS = [
    "Check this Mac",
    "Find iPhone",
    "Signing",
    "Download WebDriverAgent",
    "Build",
    "Start",
    "Connect",
]
VIEWPORT_POINTS = {"width": 393, "height": 852}
# One third of the viewport in pixels, like a scaled preview stream.
FRAME_PIXELS = {"width": 131, "height": 284}


def _frame_jpeg_base64(size: dict | None = None) -> str:
    size = size or FRAME_PIXELS
    image = Image.new("RGB", (size["width"], size["height"]), (52, 120, 246))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=70)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


FRAME_DATA = _frame_jpeg_base64()

NOT_RUNNING_SERVICE = {"reachable": False, "ready": False, "error": "connection refused"}
STATUS = {
    "supported": True,
    "started": True,
    "operations_enabled": True,
    "state_dir": "/state/iphone",
    "busy": False,
    "preview": {"paused": False, "pause_reason": None},
    "setup": {
        "ok": True,
        "configured": True,
        "config": {
            "udid": "00008110-000A1B2C3D4E5F60",
            "team_id": "******2345",
            "bundle_id": "com.example.wda",
            "local_port": 18100,
            "device_port": 8100,
        },
        "jobs": [],
        "service": NOT_RUNNING_SERVICE,
    },
}
DOCTOR_CHECKS = {
    "macos": True,
    "full_xcode": False,
    "git": True,
    "node": True,
    "npm": True,
    "configured": True,
    "selected_device_present": False,
    "paired": False,
    "developer_mode": False,
    "pinned_source": False,
}
DOCTOR = {
    "ok": False,
    "checks": DOCTOR_CHECKS,
    "devices": [],
    "next_steps": [
        "Install full Xcode, launch it to accept its license and install components, "
        "then select that Xcode with xcode-select.",
    ],
    "service": NOT_RUNNING_SERVICE,
}


def frame_payload(after_seq: int) -> dict:
    """One live preview poll: the frame is omitted once the page already has it."""
    frame = None
    if after_seq < 1:
        frame = {"seq": 1, "mimeType": "image/jpeg", "data": FRAME_DATA, **FRAME_PIXELS}
    return {
        "server_time": 1_760_000_000_000,
        "stream_id": "a" * 32,
        "device": {"model": "iPhone 15 Pro"},
        "frame": frame,
        "frame_available": True,
        "viewport": dict(VIEWPORT_POINTS),
        "busy": False,
        "paused": False,
        "pause_reason": None,
        "events": [],
    }


class IPhoneApi:
    """Answer every iPhone API request in the browser and record what the page sent."""

    def __init__(self, *, status: dict | None = None, doctor: dict | None = None) -> None:
        self.status = deepcopy(status if status is not None else STATUS)
        self.status_http = 200
        self.doctor = deepcopy(doctor if doctor is not None else DOCTOR)
        self.calls: list[tuple[str, str, dict | None]] = []
        self.unexpected: list[str] = []
        self.frame = frame_payload
        self.action_handler = None
        self.tunnel = {"available": True, "enabled": False}
        self.tunnel_handler = None

    def install(self, page) -> None:
        page.route("**/api/iphone/**", self.handle)

    def requests(self, method: str, path: str) -> list[dict | None]:
        return [body for verb, route, body in self.calls if verb == method and route == path]

    def handle(self, route) -> None:
        request = route.request
        url = urlsplit(request.url)
        body = request.post_data_json if request.method == "POST" else None
        self.calls.append((request.method, url.path, body))
        if request.method == "GET" and url.path == "/api/iphone/status":
            route.fulfill(status=self.status_http, json=self.status)
        elif request.method == "GET" and url.path == "/api/iphone/tunnel":
            route.fulfill(json=self.tunnel)
        elif request.method == "POST" and url.path == "/api/iphone/tunnel":
            if self.tunnel_handler:
                self.tunnel_handler(route, body)
            else:
                self.tunnel["enabled"] = body["enabled"]
                route.fulfill(json=self.tunnel)
        elif request.method == "POST" and url.path == "/api/iphone/doctor":
            route.fulfill(json=self.doctor)
        elif request.method == "GET" and url.path == "/api/iphone/screen/frame":
            after_seq = int(parse_qs(url.query).get("after_seq", ["0"])[0])
            route.fulfill(json=self.frame(after_seq))
        elif request.method == "POST" and url.path == "/api/iphone/action":
            if self.action_handler:
                self.action_handler(route, body)
            else:
                route.fulfill(json={"ok": True, "action_complete": True})
        else:
            self.unexpected.append(f"{request.method} {url.path}")
            route.fulfill(
                status=404,
                json={"error": {"code": "unexpected", "message": "Not mocked in this test."}},
            )


@pytest.fixture(scope="module")
def iphone_browser() -> Iterator[Browser]:
    """A disposable headless browser that never opens a signed-in profile."""
    with sync_playwright() as playwright:
        browser = fixtures._launch_disposable_browser(playwright)
        try:
            yield browser
        finally:
            browser.close()


def open_page(browser: Browser, url: str, api: IPhoneApi, *, width: int = 1280, height: int = 900):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    api.install(page)
    page.route("**/api/jury/sessions**", lambda route: route.fulfill(json={"sessions": []}))
    page.goto(url)
    return page, context, errors


def test_agent_modes_nav_offers_iphone_and_navigates_from_jurors(
    iphone_browser, sidebar_server_url,
):
    api = IPhoneApi()
    page, context, errors = open_page(
        iphone_browser, f"{sidebar_server_url}/agent/tunnel/chatgpt", api,
    )
    try:
        nav = page.locator(MODE_NAV)
        expect(nav.locator("a")).to_have_text(MODE_LABELS)
        expect(nav.locator('a[aria-current="page"]')).to_have_text("Agentic")
        expect(nav).to_have_attribute("data-option-count", "3")
        expect(nav.locator("a").last).to_have_attribute("href", "/iphone")

        page.goto(f"{sidebar_server_url}/jury/edge")
        expect(nav.locator("a")).to_have_text(MODE_LABELS)
        expect(nav.locator('a[aria-current="page"]')).to_have_text("Jurors")
        assert api.calls == []

        page.get_by_role("radio", name="iPhone", exact=True).click()
        expect(page).to_have_url(f"{sidebar_server_url}/iphone")
        expect(nav.locator("a")).to_have_text(MODE_LABELS)
        expect(nav.locator('a[aria-current="page"]')).to_have_text("iPhone")
        expect(nav.locator('a[aria-checked="true"]')).to_have_text("iPhone")
        expect(nav).to_have_attribute("data-segmented-active-index", "2")
        expect(page.locator("#iphone_workspace h2")).to_have_text("iPhone")
        assert api.requests("GET", "/api/iphone/status")
        assert not api.unexpected
        assert not errors
    finally:
        context.close()


def test_setup_steps_gate_build_and_start_on_full_xcode(iphone_browser, sidebar_server_url):
    api = IPhoneApi()
    page, context, errors = open_page(iphone_browser, f"{sidebar_server_url}/iphone", api)
    try:
        steps = page.locator(".iphone-setup-step")
        expect(steps).to_have_count(7)
        expect(steps.locator(".process-list-heading")).to_have_text(SETUP_HEADINGS)
        # The mocked status replaces the server-rendered "turned off" notice.
        expect(page.locator("[data-iphone-notice]")).to_be_hidden()

        # Status reports WebDriverAgent as not running, so the page runs the Mac check once.
        full_xcode = page.locator("[data-iphone-doctor-checks] .iphone-check").filter(has_text="Full Xcode")
        expect(full_xcode).to_have_attribute("data-check-state", "fail")
        expect(page.locator('[data-iphone-step="doctor"]')).to_have_attribute("data-step-state", "attention")
        expect(page.locator("[data-iphone-doctor-hints]")).to_contain_text("Install full Xcode")
        assert api.requests("POST", "/api/iphone/doctor") == [{}]

        build = page.locator('[data-iphone-setup-action="build"]')
        start = page.locator('[data-iphone-setup-action="start"]')
        expect(page.locator('[data-iphone-setup-action="doctor"]')).to_be_enabled()
        expect(page.locator('[data-iphone-setup-action="fetch"]')).to_be_enabled()
        expect(build).to_be_disabled()
        expect(start).to_be_disabled()
        hints = page.locator("[data-iphone-xcode-hint]")
        expect(hints).to_have_count(2)
        for index in range(2):
            expect(hints.nth(index)).to_be_visible()
            expect(hints.nth(index)).to_contain_text("Requires full Xcode")

        # A Mac check that finds full Xcode lifts the gate.
        api.doctor["checks"]["full_xcode"] = True
        page.locator('[data-iphone-setup-action="doctor"]').click()
        expect(full_xcode).to_have_attribute("data-check-state", "pass")
        expect(build).to_be_enabled()
        expect(start).to_be_enabled()
        expect(hints.first).to_be_hidden()
        assert len(api.requests("POST", "/api/iphone/doctor")) == 2
        assert not api.unexpected
        assert not errors
    finally:
        context.close()


def test_live_frame_renders_and_click_posts_a_tap_in_points(iphone_browser, sidebar_server_url):
    status = deepcopy(STATUS)
    status["setup"]["service"] = {"reachable": True, "ready": True}
    api = IPhoneApi(status=status)
    page, context, errors = open_page(iphone_browser, f"{sidebar_server_url}/iphone", api)
    try:
        root = page.locator("[data-iphone-root]")
        image = page.locator("[data-iphone-image]")
        expect(image).to_be_visible()
        expect(image).to_have_attribute("src", re.compile(r"^data:image/jpeg;base64,"))
        expect(root).to_have_attribute("data-iphone-live", "live")
        expect(root).to_have_attribute("data-frame-seq", "1")
        expect(page.locator("[data-iphone-live-text]")).to_have_text("Live")
        expect(page.locator("[data-iphone-model]")).to_have_text("iPhone 15 Pro")
        expect(page.locator("[data-iphone-screen]")).to_have_attribute("data-interactive", "true")
        page.wait_for_function(
            "() => document.querySelector('[data-iphone-image]').naturalWidth === %d"
            % FRAME_PIXELS["width"]
        )
        # Status says WebDriverAgent is ready, so no Mac check runs on load.
        assert api.requests("POST", "/api/iphone/doctor") == []

        box = image.evaluate("""element => {
            const rect = element.getBoundingClientRect();
            return {left: rect.left, top: rect.top, width: rect.width, height: rect.height};
        }""")
        # object-fit: contain may letterbox the picture inside the element box.
        scale = min(box["width"] / FRAME_PIXELS["width"], box["height"] / FRAME_PIXELS["height"])
        painted_width = FRAME_PIXELS["width"] * scale
        painted_height = FRAME_PIXELS["height"] * scale
        painted_left = box["left"] + (box["width"] - painted_width) / 2
        painted_top = box["top"] + (box["height"] - painted_height) / 2
        fraction_x, fraction_y = 0.25, 0.6
        page.mouse.click(
            painted_left + painted_width * fraction_x,
            painted_top + painted_height * fraction_y,
        )

        expect(page.locator("[data-iphone-activity]")).to_contain_text("Tapped at")
        taps = api.requests("POST", "/api/iphone/action")
        assert len(taps) == 1
        assert taps[0]["op"] == "tap"
        assert set(taps[0]["args"]) == {"x", "y"}
        point = taps[0]["args"]
        # One CSS pixel of pointer rounding, expressed in iPhone points.
        tolerance = VIEWPORT_POINTS["width"] / painted_width + 0.1
        assert abs(point["x"] - VIEWPORT_POINTS["width"] * fraction_x) <= tolerance
        assert abs(point["y"] - VIEWPORT_POINTS["height"] * fraction_y) <= tolerance
        assert round(point["x"], 1) == point["x"] and round(point["y"], 1) == point["y"]
        assert not api.unexpected
        assert not errors
    finally:
        context.close()


def test_rotation_blocks_taps_until_the_point_viewport_is_refreshed(
    iphone_browser, sidebar_server_url,
):
    status = deepcopy(STATUS)
    status["setup"]["service"] = {"reachable": True, "ready": True}
    api = IPhoneApi(status=status)
    observations = []

    def hold_observation(route, body):
        if body["op"] == "observe":
            observations.append(route)
        else:
            route.fulfill(json={"action_complete": True})

    api.action_handler = hold_observation
    page, context, errors = open_page(iphone_browser, f"{sidebar_server_url}/iphone", api)
    try:
        screen = page.locator("[data-iphone-screen]")
        expect(screen).to_have_attribute("data-interactive", "true")
        landscape = {"width": VIEWPORT_POINTS["height"], "height": VIEWPORT_POINTS["width"]}
        pixels = {"width": FRAME_PIXELS["height"], "height": FRAME_PIXELS["width"]}
        jpeg = _frame_jpeg_base64(pixels)
        current_viewport = dict(VIEWPORT_POINTS)

        def rotated_frame(after_seq):
            payload = frame_payload(after_seq)
            payload["viewport"] = dict(current_viewport)
            payload["frame"] = (
                {"seq": 2, "mimeType": "image/jpeg", "data": jpeg, **pixels}
                if after_seq < 2 else None
            )
            return payload

        api.frame = rotated_frame
        expect(page.locator("[data-iphone-device]")).to_have_attribute("data-orientation", "landscape")
        expect(screen).to_have_attribute("data-interactive", "false")
        box = screen.bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.2)
        actions = api.requests("POST", "/api/iphone/action")
        assert actions == [{"op": "observe", "args": {"mode": "tree", "max_nodes": 1}}]
        assert len(observations) == 1

        current_viewport.update(landscape)
        observations.pop().fulfill(json={"viewport": landscape, "nodes": [], "total_nodes": 0})
        expect(screen).to_have_attribute("data-interactive", "true")
        box = screen.bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.2)
        expect(page.locator("[data-iphone-activity]")).to_contain_text("Tapped at")
        actions = api.requests("POST", "/api/iphone/action")
        assert len(actions) == 2 and actions[-1]["op"] == "tap"
        tolerance = landscape["width"] / box["width"] + 0.1
        assert abs(actions[-1]["args"]["x"] - landscape["width"] * 0.5) <= tolerance
        assert abs(actions[-1]["args"]["y"] - landscape["height"] * 0.2) <= tolerance
        assert not api.unexpected
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("new_draft", [None, "A second unsent message"])
def test_typing_completion_only_clears_the_submitted_draft(
    iphone_browser, sidebar_server_url, new_draft,
):
    status = deepcopy(STATUS)
    status["setup"]["service"] = {"reachable": True, "ready": True}
    api = IPhoneApi(status=status)
    pending = []
    api.action_handler = lambda route, body: pending.append(route)
    page, context, errors = open_page(iphone_browser, f"{sidebar_server_url}/iphone", api)
    try:
        text = page.locator("[data-iphone-type-text]")
        send = page.locator("[data-iphone-type-send]")
        text.fill("The first message")
        expect(send).to_be_enabled()
        with page.expect_request("**/api/iphone/action"):
            send.click()
        expect(send).to_be_disabled()
        if new_draft is not None:
            text.fill(new_draft)
        assert api.requests("POST", "/api/iphone/action") == [
            {"op": "type_text", "args": {"text": "The first message"}},
        ]
        assert len(pending) == 1
        pending.pop().fulfill(json={"action_complete": True, "characters": 17})
        expect(page.locator("[data-iphone-activity]")).to_contain_text("Typed 17 characters.")
        expect(text).to_have_value(new_draft or "")
        assert not api.unexpected
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("path", ["/iphone", "/agent/tunnel/chatgpt"])
def test_narrow_viewport_keeps_the_mode_nav_on_one_line(iphone_browser, sidebar_server_url, path):
    api = IPhoneApi()
    page, context, errors = open_page(
        iphone_browser, f"{sidebar_server_url}{path}", api, width=390, height=844,
    )
    try:
        page.locator("#sidebar_toggle").click()
        nav = page.locator(MODE_NAV)
        expect(nav).to_be_visible()
        expect(nav.locator("a")).to_have_text(MODE_LABELS)
        geometry = nav.evaluate("""nav => {
            const options = Array.from(nav.querySelectorAll('a')).map(option => {
                const box = option.getBoundingClientRect();
                const label = option.querySelector('span');
                return {
                    top: box.top, bottom: box.bottom, left: box.left, right: box.right,
                    labelFits: label.scrollWidth <= label.clientWidth + 1
                        && option.scrollWidth <= option.clientWidth + 1,
                };
            });
            const navBox = nav.getBoundingClientRect();
            return {
                options,
                navLeft: navBox.left,
                navRight: navBox.right,
                navFits: nav.scrollWidth <= nav.clientWidth + 1,
                pageFits: document.documentElement.scrollWidth <= innerWidth + 1,
                viewportWidth: innerWidth,
            };
        }""")
        options = geometry["options"]
        assert len(options) == 3
        # One line: every option shares the first option's row.
        assert max(item["top"] for item in options) < min(item["bottom"] for item in options)
        assert all(item["labelFits"] for item in options)
        assert all(
            geometry["navLeft"] - 1 <= item["left"] and item["right"] <= geometry["navRight"] + 1
            for item in options
        )
        assert geometry["navFits"]
        assert geometry["navRight"] <= geometry["viewportWidth"]
        assert geometry["pageFits"]

        page.locator("#sidebar_toggle").click()
        assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1")
        assert not api.unexpected
        assert not errors
    finally:
        context.close()


def test_tunnel_access_only_changes_on_an_explicit_keyboard_toggle(iphone_browser, sidebar_server_url):
    api = IPhoneApi()
    page, context, errors = open_page(iphone_browser, f"{sidebar_server_url}/iphone", api)
    try:
        toggle = page.get_by_role("checkbox", name="Allow iPhone access through ChatGPT Tunnel")
        summary = page.locator("[data-iphone-tunnel-status]")
        expect(toggle).to_be_enabled()
        expect(toggle).not_to_be_checked()
        expect(summary).to_contain_text("Off.")
        heading = page.locator("#iphone_tunnel_title").bounding_box()
        label = page.locator('label[for="iphone_tunnel_enabled"]').bounding_box()
        assert heading["y"] + heading["height"] <= label["y"]
        expect(page.get_by_role("link", name="Open ChatGPT Tunnel", exact=True)).to_have_attribute(
            "href", "/agent/tunnel/chatgpt",
        )
        expect(page.locator("[data-iphone-tunnel-card]")).to_contain_text("refresh or reconnect")
        assert api.requests("POST", "/api/iphone/tunnel") == []
        toggle.focus()
        toggle.press("Space")
        expect(toggle).to_be_checked()
        expect(summary).to_contain_text("On for this app session.")
        assert api.requests("POST", "/api/iphone/tunnel") == [{"enabled": True}]

        page.reload()
        expect(toggle).to_be_enabled()
        expect(toggle).to_be_checked()
        assert api.requests("POST", "/api/iphone/tunnel") == [{"enabled": True}]
        toggle.focus()
        toggle.press("Space")
        expect(toggle).not_to_be_checked()
        expect(summary).to_contain_text("Off.")
        assert api.requests("POST", "/api/iphone/tunnel") == [{"enabled": True}, {"enabled": False}]
        assert api.requests("POST", "/api/iphone/setup") == []
        assert api.requests("POST", "/api/iphone/action") == []
        assert not api.unexpected
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("accepted", [False, True])
def test_tunnel_toggle_error_reads_back_actual_access_without_replaying(
    iphone_browser, sidebar_server_url, accepted,
):
    api = IPhoneApi()

    def fail_toggle(route, body):
        if accepted:
            api.tunnel["enabled"] = body["enabled"]
            route.abort("failed")
        else:
            route.fulfill(status=400, json={"error": {"code": "invalid_request", "message": "Access was rejected."}})

    api.tunnel_handler = fail_toggle
    page, context, errors = open_page(iphone_browser, f"{sidebar_server_url}/iphone", api)
    try:
        toggle = page.get_by_role("checkbox", name="Allow iPhone access through ChatGPT Tunnel")
        expect(toggle).to_be_enabled()
        toggle.focus()
        toggle.press("Space")
        expect(page.locator("[data-iphone-activity]")).to_contain_text("Tunnel access could not be confirmed")
        expect(toggle).to_be_enabled()
        expect(toggle).to_be_checked(checked=accepted)
        expect(page.locator("[data-iphone-tunnel-status]")).to_contain_text(
            "On for this app session." if accepted else "Off.",
        )
        assert api.requests("POST", "/api/iphone/tunnel") == [{"enabled": True}]
        assert len(api.requests("GET", "/api/iphone/tunnel")) >= 2
        assert api.requests("POST", "/api/iphone/setup") == []
        assert not api.unexpected
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("payload", [
    {"available": True},
    {"available": "true", "enabled": False},
    {"available": True, "enabled": "false"},
])
def test_tunnel_control_rejects_unconfirmed_state_shapes(
    iphone_browser, sidebar_server_url, payload,
):
    api = IPhoneApi()
    api.tunnel = payload
    page, context, errors = open_page(iphone_browser, f"{sidebar_server_url}/iphone", api)
    try:
        expect(page.locator("[data-iphone-tunnel-status]")).to_contain_text("Access status is unavailable")
        expect(page.get_by_role("checkbox", name="Allow iPhone access through ChatGPT Tunnel")).to_be_disabled()
        assert api.requests("POST", "/api/iphone/tunnel") == []
        assert not api.unexpected
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("gate", ["unsupported", "operations_disabled", "loopback_required"])
def test_tunnel_control_respects_the_page_access_gate(iphone_browser, sidebar_server_url, gate):
    api = IPhoneApi()
    if gate == "unsupported":
        api.status = {"supported": False, "started": False}
    elif gate == "operations_disabled":
        api.status = {"supported": True, "started": False, "operations_enabled": False}
    else:
        api.status_http = 403
        api.status = {"error": {"code": "loopback_required", "message": "Open this page on the Mac."}}
    page, context, errors = open_page(iphone_browser, f"{sidebar_server_url}/iphone", api)
    try:
        expect(page.locator("[data-iphone-status-label]")).to_have_text("Unavailable")
        expect(page.get_by_role("checkbox", name="Allow iPhone access through ChatGPT Tunnel")).to_be_disabled()
        assert api.requests("GET", "/api/iphone/tunnel") == []
        assert api.requests("POST", "/api/iphone/tunnel") == []
        assert not api.unexpected
        assert not errors
    finally:
        context.close()
