"""Agent Dock feedback initializes before late synchronous page scripts finish.

Code version: v1.0.0-codex.0
"""

from base64 import b64decode
import json
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import pytest
from playwright.sync_api import expect

from tests import test_dock_loading_e2e as dock_fixtures


disposable_browser = dock_fixtures.disposable_browser
dock_loading_server = dock_fixtures.dock_loading_server
pytestmark = [pytest.mark.integration, pytest.mark.slow]
_LOCATION_KEY = "cachelikes:dock-location:v1:local-resources"
_INITIAL = "/browser?view=text&source=chatgpt&q=initial&sort=newest&session_view=1"
_FINAL = "/browser?view=text&source=chatgpt&kind=all&q=remembered&sort=oldest&session_view=1"


def _open_agent(browser, fixture, *, hold_late_script):
    context = browser.new_context(
        viewport={"width": 1026, "height": 900}, reduced_motion="reduce"
    )
    context.add_init_script(
        "sessionStorage.setItem('cachelikes:sidebar-open', 'true');"
        + "sessionStorage.setItem("
        + json.dumps(_LOCATION_KEY)
        + ","
        + json.dumps(_INITIAL)
        + ");"
    )
    context.route("**/api/iphone/**", lambda route: route.fulfill(json={}))
    context.route("**/api/jury/sessions**", lambda route: route.fulfill(json={"sessions": []}))
    pending = []
    if hold_late_script:
        context.route("**/static/computer-use-agent.js*", lambda route: pending.append(route))
    page = context.new_page()
    page.goto(
        fixture.url + "/agent/tunnel/chatgpt",
        wait_until="commit" if hold_late_script else "networkidle",
    )
    expect(page.get_by_role("link", name="Local resources", exact=True)).to_be_visible()
    if hold_late_script:
        expect(page.locator('script[src*="computer-use-agent.js"]')).to_have_count(1)
        assert pending, "The late synchronous Agent script was not held."
    assert page.evaluate("typeof window.CacheWaitModal") == "object"
    assert page.evaluate("document.readyState") == (
        "loading" if hold_late_script else "complete"
    )
    return context, page


def test_agent_waiting_host_initializes_before_other_footer_scripts(dock_loading_server):
    with dock_loading_server.app.test_client() as client:
        response = client.get("/agent/tunnel/chatgpt")
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    scripts = soup.select('script[src*="waiting-modal.js"]')
    assert len(scripts) == 1
    waiting = scripts[0]
    assert waiting.find_parent("body") is not None
    assert not waiting.has_attr("defer") and not waiting.has_attr("async")
    host = soup.select_one("#cache_wait_modal")
    assert host is not None and host.find_next("script", src=True) is waiting
    footer_scripts = list(host.find_all_next("script", src=True))
    assert footer_scripts[0] is waiting
    assert any("sidebar.js?" in script["src"] for script in footer_scripts[1:])
    assert any("computer-use-agent.js?" in script["src"] for script in footer_scripts[1:])
    assert soup.select_one('[data-dock-section="local-resources"]').has_attr(
        "data-wait-navigation"
    )


@pytest.mark.parametrize("hold_late_script", [True, False], ids=["loading", "complete"])
@pytest.mark.parametrize("activation", ["click", "enter"])
def test_agent_dock_paints_while_loading_and_uses_latest_remembered_destination(
    disposable_browser, dock_loading_server, tmp_path, hold_late_script, activation
):
    fixture = dock_loading_server
    context, page = _open_agent(
        disposable_browser, fixture, hold_late_script=hold_late_script
    )
    gate = fixture.gate
    try:
        dock = page.get_by_role("link", name="Local resources", exact=True)
        assert dock.evaluate("link => link.href") == urljoin(fixture.url, _INITIAL)
        page.evaluate(
            "([key, value]) => sessionStorage.setItem(key, value)", [_LOCATION_KEY, _FINAL]
        )
        probes = []
        page.on(
            "console",
            lambda message: (
                probes.append(json.loads(message.text.removeprefix("agent-dock-wait:")))
                if message.text.startswith("agent-dock-wait:")
                else None
            ),
        )
        page.evaluate("""() => {
            window.agentDockOriginalDocument = true;
            window.addEventListener('click', event => {
                const link = event.target.closest('[data-dock-section="local-resources"]');
                if (!link) return;
                const modal = document.getElementById('cache_wait_modal');
                const box = modal.querySelector('.suggestion-loading-spinner').getBoundingClientRect();
                console.debug('agent-dock-wait:' + JSON.stringify({
                    readyState:document.readyState, hidden:modal.hidden,
                    intercepted:event.defaultPrevented, destination:link.href,
                    title:document.getElementById('cache_wait_modal_title').textContent,
                    copy:document.getElementById('cache_wait_modal_copy').textContent,
                    box:{left:box.left,top:box.top,width:box.width,height:box.height},
                }));
            }, {passive:true});
        }""")
        cdp = context.new_cdp_session(page)
        gate.hold()
        if activation == "click":
            dock.click(no_wait_after=True)
        else:
            dock.press("Enter", no_wait_after=True)
        assert gate.entered.wait(5), "The remembered destination did not reach its gate."
        screenshot = b64decode(cdp.send("Page.captureScreenshot", {"format": "png"})["data"])
        assert len(probes) == 1, probes
        proof = probes[0]
        assert proof["readyState"] == ("loading" if hold_late_script else "complete")
        assert proof["intercepted"] is True
        assert proof["hidden"] is False
        assert proof["title"] == "Opening Local resources"
        assert proof["copy"] == "Loading your saved local resources."
        assert proof["destination"] == urljoin(fixture.url, _FINAL)
        assert gate.destination == _FINAL
        blue, _pixels = dock_fixtures._painted_spinner_frame(
            screenshot, proof["box"], tmp_path / "agent-dock-loading.png"
        )
        assert blue >= 80, proof
        gate.release.set()
        page.wait_for_function("window.agentDockOriginalDocument !== true")
        page.wait_for_load_state("networkidle")
        assert page.url == urljoin(fixture.url, _FINAL)
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
    finally:
        gate.release.set()
        context.close()


@pytest.mark.parametrize(
    ("mouse_options", "attribute"),
    [
        ({"metaKey": True}, ""), ({"ctrlKey": True}, ""),
        ({"shiftKey": True}, ""), ({"altKey": True}, ""), ({"button": 1}, ""),
        ({}, "target"), ({}, "download"),
    ],
    ids=["meta", "control", "shift", "alt", "middle", "new-tab", "download"],
)
def test_agent_dock_native_actions_remain_unintercepted_during_startup(
    disposable_browser, dock_loading_server, mouse_options, attribute
):
    context, page = _open_agent(
        disposable_browser, dock_loading_server, hold_late_script=True
    )
    try:
        dock = page.get_by_role("link", name="Local resources", exact=True)
        result = dock.evaluate(
            """(link, options) => {
                if (options.attribute) {
                    link.setAttribute(options.attribute,
                        options.attribute === 'target' ? '_blank' : 'fixture');
                }
                let intercepted;
                window.addEventListener('click', event => {
                    intercepted = event.defaultPrevented;
                    event.preventDefault();
                }, {once:true});
                link.dispatchEvent(new MouseEvent('click', {
                    bubbles:true,cancelable:true,button:0,...options.mouse,
                }));
                return {intercepted,hidden:document.getElementById('cache_wait_modal').hidden};
            }""",
            {"mouse": mouse_options, "attribute": attribute},
        )
        assert result == {"intercepted": False, "hidden": True}
    finally:
        context.close()
