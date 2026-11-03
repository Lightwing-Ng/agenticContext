"""Paint shared contact-page feedback while real document responses are delayed.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from base64 import b64decode
from copy import deepcopy
import json
from threading import Event
from time import sleep
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from flask import request
import pytest
from playwright.sync_api import expect

from app.core.wechat_contact_analysis import query_contact_analysis
from tests import wechat_loading_pixels as feedback
from tests import test_wechat_contact_analysis_e2e as fixtures


disposable_browser = fixtures.disposable_browser
contact_analysis_server = fixtures.contact_analysis_server
pytestmark = [pytest.mark.integration, pytest.mark.slow]


class ContactPageGate:
    """Delay one actual route read while preserving real contact row rendering."""

    def __init__(self, fixture):
        self.ready = query_contact_analysis(
            fixture.store,
            fixture.target_id,
            query="target statement",
            background=False,
        )
        assert self.ready["status"] == "ready"
        assert len(self.ready["items"]) == 50
        self.armed = Event()
        self.entered = Event()
        self.release = Event()
        self.destination = ""

    def hold(self):
        self.destination = ""
        self.entered.clear()
        self.release.clear()
        self.armed.set()

    def __call__(self, *_args, **kwargs):
        if self.armed.is_set():
            self.armed.clear()
            self.destination = request.full_path.removesuffix("?")
            self.entered.set()
            if not self.release.wait(15):
                raise TimeoutError("The isolated pagination response was not released.")
        # Model a distant page without manufacturing another 117,000 source records.
        result = deepcopy(self.ready)
        selected = min(2349, max(1, int(kwargs.get("page", 1))))
        result.update(total_count=117407, total_pages=2349, current_page=selected)
        result["counts"]["messages"] = 117407
        if selected == 2349:
            result["items"] = result["items"][:7]
        return result


@pytest.fixture(scope="module")
def contact_pagination_server(contact_analysis_server):
    fixture = contact_analysis_server
    gate = ContactPageGate(fixture)
    with patch(
        "app.core.wechat_contact_analysis.query_contact_analysis", side_effect=gate
    ):
        yield SimpleNamespace(**vars(fixture), gate=gate)
    gate.release.set()
    assert fixture.before == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    )


def _open_contact(
    browser,
    fixture,
    *,
    page_number=2348,
    width=1026,
    scheme="light",
    motion="reduce",
    sort="newest",
):
    context = browser.new_context(
        viewport={"width": width, "height": 900},
        color_scheme=scheme,
        reduced_motion=motion,
    )
    page = context.new_page()
    return_url = (
        "/browser?view=text&source=wechat&account=account%3Awxid_account_a"
        "&chat_kind=group&session=" + fixture.session + "&sort=oldest"
        "#wechat_message_preserved"
    )
    scope = {"q": "target statement", "sort": sort, "return_to": return_url}
    url = fixture.url + "/browser/wechat/contacts/" + fixture.target_id
    page.goto(
        url + "?" + urlencode({**scope, "page": page_number}), wait_until="networkidle"
    )
    expect(page.locator("#cache_wait_modal")).to_be_hidden()
    return context, page, scope


@pytest.mark.parametrize(
    ("initial", "label", "target", "width", "scheme", "motion", "activation", "sort"),
    [
        (
            2348,
            "Statement page 2349",
            2349,
            1026,
            "light",
            "no-preference",
            "click",
            "newest",
        ),
        (2349, "Statement page 2349", 2349, 390, "dark", "reduce", "click", "newest"),
        (
            2349,
            "Previous statement page",
            2345,
            390,
            "light",
            "no-preference",
            "click",
            "oldest",
        ),
        (2349, "Statement page 2346", 2346, 1026, "dark", "reduce", "enter", "oldest"),
        (2345, "Next statement page", 2346, 1026, "light", "reduce", "click", "newest"),
    ],
    ids=[
        "deep-number",
        "active-page-reload",
        "narrow-previous",
        "keyboard-number",
        "next-chunk",
    ],
)
def test_contact_pagination_paints_during_real_delayed_navigation(
    disposable_browser,
    contact_pagination_server,
    tmp_path,
    initial,
    label,
    target,
    width,
    scheme,
    motion,
    activation,
    sort,
):
    fixture = contact_pagination_server
    context, page, scope = _open_contact(
        disposable_browser,
        fixture,
        page_number=initial,
        width=width,
        scheme=scheme,
        motion=motion,
        sort=sort,
    )
    gate = fixture.gate
    try:
        pager = page.get_by_role("navigation", name="Contact statement pages")
        link = pager.get_by_role("link", name=label, exact=True)
        if target == initial:
            # Normalize parameter order first, then exercise a byte-identical URL reload.
            page.goto(
                urljoin(fixture.url, link.get_attribute("href")),
                wait_until="networkidle",
            )
            assert page.url == urljoin(fixture.url, link.get_attribute("href"))
        expected_destination = urlsplit(
            urljoin(fixture.url, link.get_attribute("href"))
        )
        before = page.url
        probes = []
        page.on(
            "console",
            lambda message: (
                probes.append(
                    json.loads(message.text.removeprefix("contact-page-wait:"))
                )
                if message.text.startswith("contact-page-wait:")
                else None
            ),
        )
        page.evaluate("""() => {
            window.paginationOriginalDocument = true;
            // Observe the completed production click listener without cancelling navigation.
            window.addEventListener('click', event => {
                const link = event.target.closest('a');
                if (!link?.closest('nav[aria-label="Contact statement pages"]')) return;
                const modal = document.getElementById('cache_wait_modal');
                const spinner = modal.querySelector('.suggestion-loading-spinner');
                const style = getComputedStyle(spinner);
                const box = spinner.getBoundingClientRect();
                console.debug('contact-page-wait:' + JSON.stringify({
                    hidden: modal.hidden,
                    title: document.getElementById('cache_wait_modal_title').textContent,
                    copy: document.getElementById('cache_wait_modal_copy').textContent,
                    original: window.paginationOriginalDocument === true,
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
            link.press("Enter", no_wait_after=True)
        else:
            link.click(no_wait_after=True)
        assert gate.entered.wait(5), (
            "The real contact-page request did not reach the gate."
        )
        screenshot = b64decode(
            cdp.send("Page.captureScreenshot", {"format": "png"})["data"]
        )
        assert len(probes) == 1, probes
        pending = probes[0]
        assert pending["hidden"] is False
        assert pending["title"] == "Loading statements"
        assert pending["copy"] == "Opening the selected page of contact statements."
        assert pending["original"] is True
        assert (
            gate.destination
            == expected_destination.path + "?" + expected_destination.query
        )
        first_blue, first_pixels = feedback._painted_spinner_frame(
            screenshot, pending["box"], tmp_path / "contact-page-wait-first.png"
        )
        assert first_blue >= 80, pending
        assert pending["overflow"] <= 1, pending
        if motion == "reduce":
            assert pending["animation"] == "none"
        else:
            assert pending["animation"] == "ticker-suggestion-loading"
            assert pending["playState"] == "running"
            sleep(0.1)
            second = b64decode(
                cdp.send("Page.captureScreenshot", {"format": "png"})["data"]
            )
            second_blue, second_pixels = feedback._painted_spinner_frame(
                second, pending["box"], tmp_path / "contact-page-wait-second.png"
            )
            assert second_blue >= 80
            assert second_pixels != first_pixels
        gate.release.set()
        page.wait_for_function("window.paginationOriginalDocument !== true")
        page.wait_for_load_state("networkidle")
        values = parse_qs(urlsplit(page.url).query)
        assert values["page"] == [str(target)]
        for key, value in scope.items():
            assert values[key] == [value]
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        expect(
            page.get_by_role("link", name=f"Statement page {target}", exact=True)
        ).to_have_attribute("aria-current", "page")
        if target != initial:
            page.go_back(wait_until="networkidle")
            assert page.url == before
            expect(page.locator("#cache_wait_modal")).to_be_hidden()
            page.go_forward(wait_until="networkidle")
            assert parse_qs(urlsplit(page.url).query)["page"] == [str(target)]
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
def test_modified_contact_pagination_keeps_native_navigation(
    disposable_browser, contact_pagination_server, click_options
):
    context, page, scope = _open_contact(disposable_browser, contact_pagination_server)
    try:
        link = page.get_by_role("link", name="Statement page 2349", exact=True)
        result = link.evaluate(
            """(link, options) => {
                let intercepted = null;
                // Suppress only this synthetic modified action after observing production.
                window.addEventListener('click', event => {
                    intercepted = event.defaultPrevented;
                    event.preventDefault();
                }, {once:true});
                link.dispatchEvent(new MouseEvent('click', {
                    bubbles:true, cancelable:true, button:0, ...options,
                }));
                return {intercepted, hidden:document.getElementById('cache_wait_modal').hidden,
                    href:link.href};
            }""",
            click_options,
        )
        assert result["intercepted"] is False
        assert result["hidden"] is True
        values = parse_qs(urlsplit(result["href"]).query)
        assert values["page"] == ["2349"]
        for key, value in scope.items():
            assert values[key] == [value]
    finally:
        context.close()
