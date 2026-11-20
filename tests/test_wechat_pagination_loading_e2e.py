"""Paint WeChat page feedback before delayed document navigation completes.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from base64 import b64decode
import json
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from flask import Flask, render_template_string, request
import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.local_media_browser import build_local_store_pagination
from tests import test_dock_loading_e2e as feedback


ROOT = Path(__file__).resolve().parents[1]
disposable_browser = feedback.disposable_browser
pytestmark = [pytest.mark.integration, pytest.mark.slow]
SCOPE = {
    "view": "text",
    "source": "wechat",
    "account": "account:fixture-owner",
    "chat_kind": "group",
    "q": "preserved query",
    "sort": "oldest",
    "session_view": "1",
    "session": "wechat:fixture-conversation",
    "session_page": "7",
}


@pytest.fixture(scope="module")
def wechat_pagination_server():
    """Render the production partial and controllers without opening an archive."""
    app = Flask(
        __name__,
        template_folder=str(ROOT / "app/web/templates"),
        static_folder=str(ROOT / "app/web/static"),
    )
    gate = feedback.NavigationGate()
    app.before_request(gate.before_request)

    def browser():
        current = min(821, max(1, int(request.args.get("page", 821))))
        page = SimpleNamespace(
            pagination_items=build_local_store_pagination(821, current),
            current_page=current,
            session_detail=True,
            session_view=True,
            pagination_unit="page",
        )
        filters = SimpleNamespace(**{**SCOPE, "project": "", "answerer": ""})
        return render_template_string(
            """<!doctype html><html lang="en"><head>
            <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
            </head><body>
            <main style="min-height:100vh;display:grid;place-items:end center;padding:24px">
            {% from "_pagination.html" import render_browser_pagination %}
            {{ render_browser_pagination(page, "Message pages", "text", filters, "messages") }}
            </main>
            {% include "_waiting_modal.html" %}
            <script type="application/json" id="browser_media_data">[]</script>
            <dialog id="browser_detail_dialog"></dialog>
            <script src="{{ url_for('static', filename='waiting-modal.js') }}"></script>
            <script src="{{ url_for('static', filename='local-media-browser.js') }}"></script>
            </body></html>""",
            page=page,
            filters=filters,
        )

    app.add_url_rule("/browser", endpoint="local_resources.browser", view_func=browser)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    assert server.server_port != 8666
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", gate=gate)
    finally:
        gate.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _open_pagination(browser, fixture, initial=821, width=834):
    context = browser.new_context(
        viewport={"width": width, "height": 1086}, reduced_motion="reduce"
    )
    page = context.new_page()
    page.goto(
        fixture.url + "/browser?" + urlencode({**SCOPE, "page": initial}),
        wait_until="networkidle",
    )
    expect(page.locator("#cache_wait_modal")).to_be_hidden()
    return context, page


@pytest.mark.parametrize(
    ("selection", "width"),
    [
        ("number", 834),
        ("range", 834),
        ("current", 834),
        ("next", 1280),
        ("range", 1280),
    ],
)
def test_wechat_pagination_paints_before_delayed_navigation(
    disposable_browser, wechat_pagination_server, tmp_path, selection, width
):
    fixture = wechat_pagination_server
    context, page = _open_pagination(
        disposable_browser,
        fixture,
        initial=820 if selection == "next" else 821,
        width=width,
    )
    gate = fixture.gate
    try:
        pager = page.get_by_role("navigation", name="Message pages")
        if selection == "range":
            pager.get_by_role("button", name="Show earlier messages").click()
            expect(page.locator("#cache_wait_modal")).to_be_hidden()
            expect(pager.locator("[data-pagination-range-trigger]")).to_have_attribute(
                "aria-expanded", "true"
            )
            link = pager.get_by_role("menuitem").first
        elif selection == "next":
            link = pager.get_by_role("link", name="Next message page", exact=True)
        else:
            number = 821 if selection == "current" else 1
            link = pager.get_by_role("link", name=f"Message page {number}", exact=True)
        expected = urlsplit(urljoin(fixture.url, link.get_attribute("href")))
        probes = []
        page.on(
            "console",
            lambda message: (
                probes.append(
                    json.loads(message.text.removeprefix("wechat-page-wait:"))
                )
                if message.text.startswith("wechat-page-wait:")
                else None
            ),
        )
        page.evaluate("""() => {
            window.wechatPaginationOriginalDocument = true;
            window.addEventListener('click', event => {
                if (!event.target.closest('.browser-pagination a')) return;
                const modal = document.getElementById('cache_wait_modal');
                const box = modal.querySelector('.suggestion-loading-spinner').getBoundingClientRect();
                console.debug('wechat-page-wait:' + JSON.stringify({
                    hidden: modal.hidden,
                    title: document.getElementById('cache_wait_modal_title').textContent,
                    copy: document.getElementById('cache_wait_modal_copy').textContent,
                    intercepted: event.defaultPrevented,
                    box: {left:box.left,top:box.top,width:box.width,height:box.height},
                }));
            }, {passive:true});
        }""")
        cdp = context.new_cdp_session(page)
        gate.hold()
        if selection == "number":
            link.press("Enter", no_wait_after=True)
        else:
            link.click(no_wait_after=True)
        assert gate.entered.wait(5), "The isolated page request did not reach its gate."
        screenshot = b64decode(
            cdp.send("Page.captureScreenshot", {"format": "png"})["data"]
        )
        assert len(probes) == 1, probes
        pending = probes[0]
        assert pending["hidden"] is False
        assert pending["intercepted"] is True
        assert pending["title"] == "Loading messages"
        assert pending["copy"] == "Opening the selected page of this conversation."
        assert gate.destination == expected.path + "?" + expected.query
        blue_pixels, _ = feedback._painted_spinner_frame(
            screenshot, pending["box"], tmp_path / "wechat-page-wait.png"
        )
        assert blue_pixels >= 80
        gate.release.set()
        page.wait_for_function("window.wechatPaginationOriginalDocument !== true")
        page.wait_for_load_state("networkidle")
        assert urlsplit(page.url) == expected
        params = parse_qs(expected.query)
        for key, value in SCOPE.items():
            assert params[key] == [value]
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
    finally:
        gate.release.set()
        context.close()


@pytest.mark.parametrize(
    ("current", "direction", "range_name", "expected", "action"),
    [
        (821, "earlier", "Messages 816 through 820", 820, "click"),
        (821, "earlier", "Messages 816 through 820", 820, "keyboard"),
        (7, "later", "Messages 11 through 15", 11, "click"),
        (7, "later", "Messages 11 through 15", 11, "keyboard"),
    ],
)
def test_wechat_range_selection_opens_nearest_page(
    disposable_browser, wechat_pagination_server,
    current, direction, range_name, expected, action,
):
    context, page = _open_pagination(
        disposable_browser, wechat_pagination_server, initial=current
    )
    try:
        pager = page.get_by_role("navigation", name="Message pages")
        pager.get_by_role("button", name=f"Show {direction} messages").click()
        option = pager.get_by_role("menuitem", name=range_name)
        href = option.get_attribute("href")
        assert parse_qs(urlsplit(href).query)["page"] == [str(expected)]
        if action == "keyboard":
            option.focus()
            option.press("Enter")
        else:
            option.click()
        page.wait_for_url(
            lambda url: parse_qs(urlsplit(str(url)).query).get("page")
            == [str(expected)]
        )
        assert parse_qs(urlsplit(page.url).query)["page"] == [str(expected)]
        expect(
            page.get_by_role("navigation", name="Message pages")
            .locator("[aria-current='page']")
        ).to_have_text(str(expected))
    finally:
        context.close()


@pytest.mark.parametrize(
    "native_action", ["ctrl", "meta", "shift", "alt", "target", "download"]
)
def test_wechat_pagination_preserves_native_link_actions(
    disposable_browser, wechat_pagination_server, native_action
):
    context, page = _open_pagination(disposable_browser, wechat_pagination_server)
    try:
        link = page.get_by_role("link", name="Message page 1", exact=True)
        result = link.evaluate(
            """(link, action) => {
                if (action === 'target') link.target = '_blank';
                if (action === 'download') link.download = 'fixture';
                let intercepted;
                window.addEventListener('click', event => {
                    intercepted = event.defaultPrevented;
                    event.preventDefault();
                }, {once:true});
                link.dispatchEvent(new MouseEvent('click', {
                    bubbles:true, cancelable:true, button:0,
                    ctrlKey:action === 'ctrl', metaKey:action === 'meta',
                    shiftKey:action === 'shift', altKey:action === 'alt',
                }));
                return {intercepted, hidden:document.getElementById('cache_wait_modal').hidden};
            }""",
            native_action,
        )
        assert result == {"intercepted": False, "hidden": True}
    finally:
        context.close()
