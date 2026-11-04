"""Paint shared Dock feedback before genuinely delayed document navigation.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from base64 import b64decode
from io import BytesIO
import json
from threading import Event, Thread
from time import sleep
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urljoin, urlsplit

from flask import request
from PIL import Image
import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.config import CrawlConfig
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e as sidebar_fixtures


disposable_browser = sidebar_fixtures.disposable_browser
pytestmark = [pytest.mark.integration, pytest.mark.slow]


class NavigationGate:
    """Hold the actual destination response without cancelling browser navigation."""

    def __init__(self):
        self.armed = Event()
        self.entered = Event()
        self.release = Event()
        self.destination = ""

    def hold(self):
        self.entered.clear()
        self.release.clear()
        self.destination = ""
        self.armed.set()

    def before_request(self):
        if request.path != "/browser" or not self.armed.is_set():
            return None
        self.armed.clear()
        self.destination = request.full_path.removesuffix("?")
        self.entered.set()
        if not self.release.wait(15):
            return "The isolated navigation gate was not released.", 504
        return None


def _painted_spinner_frame(screenshot, box, path):
    """Read compositor pixels without evaluating a provisional document context."""
    path.write_bytes(screenshot)
    center_x = box["left"] + box["width"] / 2
    center_y = box["top"] + box["height"] / 2
    crop = (
        Image.open(BytesIO(screenshot))
        .convert("RGB")
        .crop(
            (
                round(center_x - 26),
                round(center_y - 26),
                round(center_x + 26),
                round(center_y + 26),
            )
        )
    )
    pixels = crop.tobytes()
    blue_pixels = sum(
        blue > 120 and blue > red + 50 and blue > green + 20
        for red, green, blue in zip(
            pixels[::3], pixels[1::3], pixels[2::3], strict=True
        )
    )
    return blue_pixels, pixels


@pytest.fixture(scope="module")
def dock_loading_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("dock-loading-browser")
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            sandbox / "store",
            computer_use_settings_path=sandbox / "agent.json",
            computer_use_runtime_root=sandbox / "runtime",
            agent_external_operations_enabled=False,
            beta_enabled=True,
        )
    app.config.update(TESTING=True)
    gate = NavigationGate()
    app.before_request(gate.before_request)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}", gate=gate, app=app
        )
    finally:
        gate.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.extensions["runtime_shutdown"]()


def _open_entry(
    browser, fixture, route, *, width=1026, scheme="light", motion="reduce"
):
    context = browser.new_context(
        viewport={"width": width, "height": 900},
        color_scheme=scheme,
        reduced_motion=motion,
    )
    # The Dock test never needs native device discovery or browser-owned jurors.
    context.route("**/api/iphone/**", lambda route: route.fulfill(json={}))
    context.route(
        "**/api/jury/sessions**", lambda route: route.fulfill(json={"sessions": []})
    )
    page = context.new_page()
    page.goto(fixture.url + route, wait_until="networkidle")
    expect(page.locator("#cache_wait_modal")).to_be_hidden()
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
    return context, page


@pytest.mark.parametrize(
    "route",
    [
        "/agent/tunnel/chatgpt",
        "/cache/chatgpt",
        "/browser?view=text",
        "/settings",
        "/beta",
        "/jury/edge",
        "/iphone",
        "/settings/style-tokens",
    ],
)
def test_every_dock_entrypoint_has_one_shared_waiting_host(dock_loading_server, route):
    with dock_loading_server.app.test_client() as client:
        response = client.get(route, follow_redirects=True)
    assert response.status_code == 200
    document = response.get_data(as_text=True)
    assert document.count('id="cache_wait_modal"') == 1
    assert document.count("waiting-modal.js?") == 1


@pytest.mark.parametrize(
    ("route", "width", "scheme", "motion", "activation"),
    [
        ("/cache/chatgpt", 1026, "light", "no-preference", "click"),
        ("/browser?q=&sort=newest&view=text", 1026, "dark", "no-preference", "click"),
        ("/beta", 390, "light", "reduce", "click"),
        ("/jury/edge", 390, "dark", "no-preference", "click"),
        ("/iphone", 1026, "light", "reduce", "click"),
        ("/settings/style-tokens", 390, "dark", "reduce", "click"),
        ("/cache/chatgpt", 1026, "light", "reduce", "enter"),
    ],
    ids=["cache", "active-dock", "beta", "jury", "iphone", "style-catalog", "keyboard"],
)
def test_local_resources_dock_paints_before_delayed_navigation_and_clears_on_return(
    disposable_browser,
    dock_loading_server,
    tmp_path,
    route,
    width,
    scheme,
    motion,
    activation,
):
    fixture = dock_loading_server
    context, page = _open_entry(
        disposable_browser, fixture, route, width=width, scheme=scheme, motion=motion
    )
    gate = fixture.gate
    try:
        dock = page.get_by_role("link", name="Local resources", exact=True)
        if route.startswith("/browser"):
            # Exercise an actual same-URL active-Dock reload, after normal URL memory.
            remembered = dock.get_attribute("href")
            page.goto(urljoin(fixture.url, remembered), wait_until="networkidle")
            dock = page.get_by_role("link", name="Local resources", exact=True)
            assert page.url == urljoin(fixture.url, dock.get_attribute("href"))
        destination = urlsplit(urljoin(fixture.url, dock.get_attribute("href")))
        expected_destination = destination.path + (
            "?" + destination.query if destination.query else ""
        )
        before = page.url
        probes = []
        page.on(
            "console",
            lambda message: (
                probes.append(json.loads(message.text.removeprefix("dock-wait-proof:")))
                if message.text.startswith("dock-wait-proof:")
                else None
            ),
        )
        page.evaluate("""() => {
            window.dockLoadingOriginalDocument = true;
            // This passive observer never cancels or delays the real navigation.
            window.addEventListener('click', event => {
                if (!event.target.closest('[data-dock-section="local-resources"]')) return;
                const spinner = document.querySelector('#cache_wait_modal .suggestion-loading-spinner');
                const style = getComputedStyle(spinner);
                const box = spinner.getBoundingClientRect();
                console.debug('dock-wait-proof:' + JSON.stringify({
                    hidden: document.getElementById('cache_wait_modal').hidden,
                    title: document.getElementById('cache_wait_modal_title').textContent,
                    copy: document.getElementById('cache_wait_modal_copy').textContent,
                    originalDocument: window.dockLoadingOriginalDocument === true,
                    animation: style.animationName,
                    playState: spinner.getAnimations()[0]?.playState || null,
                    overflow: document.documentElement.scrollWidth - innerWidth,
                    box: {left:box.left,top:box.top,width:box.width,height:box.height},
                }));
            }, {passive:true});
        }""")
        cdp = context.new_cdp_session(page)
        gate.hold()
        if activation == "enter":
            dock.press("Enter", no_wait_after=True)
        else:
            dock.click(no_wait_after=True)
        assert gate.entered.wait(5), "The real document request did not reach the gate."
        first_screenshot = b64decode(
            cdp.send("Page.captureScreenshot", {"format": "png"})["data"]
        )
        assert len(probes) == 1, probes
        pending = probes[0]
        assert pending["hidden"] is False
        assert pending["title"] == "Opening Local resources"
        assert pending["copy"] == "Loading your saved local resources."
        assert pending["originalDocument"] is True
        assert gate.destination == expected_destination
        first_blue, first_pixels = _painted_spinner_frame(
            first_screenshot, pending["box"], tmp_path / "dock-wait-first.png"
        )
        assert first_blue >= 80, pending
        assert pending["overflow"] <= 1, pending
        if motion == "reduce":
            assert pending["animation"] == "none"
        else:
            assert pending["animation"] == "ticker-suggestion-loading"
            assert pending["playState"] == "running"
            sleep(0.1)
            second_screenshot = b64decode(
                cdp.send("Page.captureScreenshot", {"format": "png"})["data"]
            )
            second_blue, second_pixels = _painted_spinner_frame(
                second_screenshot, pending["box"], tmp_path / "dock-wait-second.png"
            )
            assert second_blue >= 80
            assert second_pixels != first_pixels
        gate.release.set()
        page.wait_for_function("window.dockLoadingOriginalDocument !== true")
        page.wait_for_load_state("networkidle")
        assert urlsplit(page.url).path == "/browser"
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        if not route.startswith("/browser"):
            page.go_back(wait_until="networkidle")
            assert page.url == before
            expect(page.locator("#cache_wait_modal")).to_be_hidden()
            page.go_forward(wait_until="networkidle")
            assert urlsplit(page.url).path == "/browser"
            expect(page.locator("#cache_wait_modal")).to_be_hidden()
    finally:
        gate.release.set()
        context.close()


@pytest.mark.parametrize(
    "click_options",
    [
        {"metaKey": True},
        {"ctrlKey": True},
        {"shiftKey": True},
        {"altKey": True},
        {"button": 1},
    ],
    ids=["meta", "control", "shift", "alt", "middle"],
)
def test_modified_dock_activation_does_not_intercept_native_action(
    disposable_browser, dock_loading_server, click_options
):
    context, page = _open_entry(
        disposable_browser, dock_loading_server, "/cache/chatgpt"
    )
    try:
        result = page.get_by_role("link", name="Local resources", exact=True).evaluate(
            """(link, options) => {
                let intercepted = null;
                // Observe the production listener, then stop only this synthetic action.
                window.addEventListener('click', event => {
                    intercepted = event.defaultPrevented;
                    event.preventDefault();
                }, {once: true});
                link.dispatchEvent(new MouseEvent('click', {
                    bubbles: true, cancelable: true, button: 0, ...options,
                }));
                return {intercepted, hidden: document.getElementById('cache_wait_modal').hidden};
            }""",
            click_options,
        )
        assert result == {"intercepted": False, "hidden": True}
    finally:
        context.close()


@pytest.mark.parametrize("attribute", ["target", "download"])
def test_non_document_dock_action_retains_native_handling(
    disposable_browser, dock_loading_server, attribute
):
    context, page = _open_entry(
        disposable_browser, dock_loading_server, "/cache/chatgpt"
    )
    try:
        result = page.get_by_role("link", name="Local resources", exact=True).evaluate(
            """(link, attribute) => {
                link.setAttribute(attribute, attribute === 'target' ? '_blank' : 'fixture');
                let intercepted = null;
                window.addEventListener('click', event => {
                    intercepted = event.defaultPrevented;
                    event.preventDefault();
                }, {once: true});
                link.dispatchEvent(new MouseEvent('click', {
                    bubbles: true, cancelable: true, button: 0,
                }));
                return {intercepted, hidden: document.getElementById('cache_wait_modal').hidden};
            }""",
            attribute,
        )
        assert result == {"intercepted": False, "hidden": True}
    finally:
        context.close()


@pytest.mark.parametrize(
    "fragment", ["#", "#foo", "#bar"], ids=["empty", "same", "new"]
)
def test_same_document_fragment_does_not_leave_a_waiting_overlay(
    disposable_browser, dock_loading_server, fragment
):
    context, page = _open_entry(
        disposable_browser, dock_loading_server, "/cache/chatgpt#foo"
    )
    try:
        page.evaluate(
            """fragment => {
                const dock = document.querySelector('[data-dock-section="local-resources"]');
                // Exercise the reusable link adapter without Dock URL-memory rewriting.
                const link = document.createElement('a');
                link.id = 'fragment-wait-fixture';
                link.href = fragment;
                link.textContent = 'Fragment fixture';
                for (const name of ['data-wait-navigation', 'data-wait-title', 'data-wait-copy']) {
                    link.setAttribute(name, dock.getAttribute(name));
                }
                Object.assign(link.style, {
                    position:'fixed',left:'50%',top:'10px',zIndex:'2147483647',
                });
                document.body.appendChild(link);
                window.dockFragmentOriginalDocument = true;
                window.addEventListener('click', event => {
                    if (event.target === link) {
                        window.dockFragmentIntercepted = event.defaultPrevented;
                    }
                }, {passive:true});
            }""",
            fragment,
        )
        page.locator("#fragment-wait-fixture").click(no_wait_after=True)
        page.wait_for_url(
            lambda url: urlsplit(url).fragment == fragment.removeprefix("#")
        )
        assert page.evaluate(
            """({
                original: window.dockFragmentOriginalDocument,
                intercepted: window.dockFragmentIntercepted,
                hidden: document.getElementById('cache_wait_modal').hidden,
            })"""
        ) == {"original": True, "intercepted": False, "hidden": True}
    finally:
        context.close()
