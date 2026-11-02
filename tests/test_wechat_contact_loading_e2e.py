"""Contact loading keeps one stable shared dialog and never refreshes the document.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import json
from threading import Event, Lock
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from flask import request
import pytest
from playwright.sync_api import expect

from app.core.wechat_contact_analysis import query_contact_analysis
from tests import test_wechat_contact_analysis_e2e as fixtures


disposable_browser = fixtures.disposable_browser
contact_analysis_server = fixtures.contact_analysis_server


class ControlledContactResults:
    """Keep real route rendering while controlling only the source-read lifecycle."""

    def __init__(self, fixture):
        self.ready = query_contact_analysis(
            fixture.store, fixture.target_id, background=False
        )
        self.release = Event()
        self.fail = Event()
        self._calls = []
        self._lock = Lock()

    def __call__(self, *_args, **_kwargs):
        with self._lock:
            self._calls.append(
                {"accept": request.headers.get("Accept", ""), "url": request.full_path}
            )
        result = deepcopy(self.ready)
        if self.fail.is_set():
            result.update(status="error", error="Synthetic source failure")
        elif self.release.is_set():
            return result
        else:
            result["status"] = "indexing"
        result.update(
            items=[],
            total_count=0,
            current_page=1,
            total_pages=1,
            counts={
                "messages": 0,
                "conversations": 0,
                "source_accounts": 0,
                "sources": 0,
            },
        )
        return result

    def json_requests(self):
        with self._lock:
            return [
                call for call in self._calls if "application/json" in call["accept"]
            ]


LOADING_PROBE = """(() => {
    const probe = window.__contactLoadingProbe = {
        firstModalHidden: null, hiddenBeforeReady: 0, modalReplaced: false,
        cardReplaced: false, contentReplacedWhilePending: false, tableAppearances: 0,
        allowFinish: false, zeroMetricWhilePending: false,
    };
    let initialModal = null;
    let initialCard = null;
    let initialContent = null;
    let previousTable = null;
    new MutationObserver(() => {
        const modal = document.getElementById('cache_wait_modal');
        const card = modal?.querySelector('.workspace-modal-dialog');
        const content = document.querySelector('[data-contact-analysis]');
        if (modal && !initialModal) {
            initialModal = modal;
            probe.firstModalHidden = modal.hidden;
        }
        if (card && !initialCard) initialCard = card;
        if (content && !initialContent) initialContent = content;
        if (initialModal && modal !== initialModal) probe.modalReplaced = true;
        if (initialCard && card !== initialCard) probe.cardReplaced = true;
        if (!probe.allowFinish) {
            if (initialModal && modal?.hidden) probe.hiddenBeforeReady++;
            if (initialContent && content !== initialContent) probe.contentReplacedWhilePending = true;
            if ([...document.querySelectorAll('.browser-metric-grid strong')]
                .some(node => node.textContent.trim() === '0')) probe.zeroMetricWhilePending = true;
        }
        const table = document.querySelector('[data-contact-analysis] table[data-table-body]');
        if (table && table !== previousTable) probe.tableAppearances++;
        previousTable = table;
    }).observe(document, {subtree: true, childList: true, attributes: true, attributeFilter: ['hidden']});
})();"""


def _contact_url(fixture):
    return fixture.url + "/browser/wechat/contacts/" + fixture.target_id


def _dialog_geometry(page):
    return page.locator("#cache_wait_modal .workspace-modal-dialog").evaluate(
        """dialog => {
        const box = dialog.getBoundingClientRect();
        const style = getComputedStyle(dialog);
        const spinner = dialog.querySelector('.suggestion-loading-spinner');
        const spinnerStyle = getComputedStyle(spinner);
        const spinnerBox = spinner.getBoundingClientRect();
        return {left: box.left, top: box.top, width: box.width, height: box.height,
            centerX: Math.abs(box.left + box.width / 2 - innerWidth / 2),
            centerY: Math.abs(box.top + box.height / 2 - innerHeight / 2),
            background: style.backgroundColor, blur: style.backdropFilter,
            borderRadius: style.borderTopLeftRadius,
            expectedRadius: style.getPropertyValue('--workspace-modal-radius').trim(),
            shadow: style.boxShadow, svgCount: spinner.querySelectorAll('svg').length,
            circleCount: spinner.querySelectorAll('svg circle').length,
            spinnerWidth: spinnerBox.width, spinnerHeight: spinnerBox.height,
            animation: spinnerStyle.animationName, duration: spinnerStyle.animationDuration,
            timing: spinnerStyle.animationTimingFunction,
            iterations: spinnerStyle.animationIterationCount,
            documentOverflow: document.documentElement.scrollWidth - innerWidth};
    }"""
    )


def _assert_stable_dialog(samples):
    first = samples[0]
    for sample in samples:
        assert sample["centerX"] <= 1 and sample["centerY"] <= 1, sample
        assert sample["documentOverflow"] <= 1, sample
        assert sample["svgCount"] == 1 and sample["circleCount"] == 2, sample
        assert sample["spinnerWidth"] > 0 and sample["spinnerHeight"] > 0, sample
        assert sample["background"] != "rgba(0, 0, 0, 0)", sample
        assert sample["blur"] != "none" and sample["shadow"] != "none", sample
        assert sample["borderRadius"] == sample["expectedRadius"], sample
        for field in ("left", "top", "width", "height"):
            assert abs(sample[field] - first[field]) <= 1, samples


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_contact_loading_keeps_a_centered_shared_dialog_without_document_flashes(
    disposable_browser, contact_analysis_server, tmp_path, width, color_scheme, motion
):
    fixture = contact_analysis_server
    controlled = ControlledContactResults(fixture)
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        reduced_motion=motion,
        color_scheme=color_scheme,
    )
    page = context.new_page()
    page.add_init_script(LOADING_PROBE)
    document_requests = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "request",
        lambda incoming: (
            document_requests.append(incoming.url)
            if incoming.resource_type == "document"
            else None
        ),
    )
    try:
        with patch(
            "app.core.wechat_contact_analysis.query_contact_analysis",
            side_effect=controlled,
        ):
            response = page.goto(_contact_url(fixture), wait_until="domcontentloaded")
            assert response.status == 202
            assert "refresh" not in response.headers
            modal = page.locator("#cache_wait_modal")
            expect(modal).to_be_visible()
            expect(modal).to_have_attribute("role", "dialog")
            expect(modal).to_have_attribute("aria-modal", "true")
            page.evaluate("() => document.fonts.ready")
            history_length = page.evaluate("history.length")
            samples = []
            for expected in (1, 2, 3):
                page.wait_for_function(
                    "expected => performance.getEntriesByType('resource').filter(entry => "
                    "entry.initiatorType === 'fetch' && entry.name.includes('/browser/wechat/contacts/')).length >= expected",
                    arg=expected,
                    timeout=12000,
                )
                assert len(controlled.json_requests()) >= expected
                expect(modal).to_be_visible()
                samples.append(_dialog_geometry(page))
            _assert_stable_dialog(samples)
            if motion == "reduce":
                assert samples[0]["animation"] == "none", samples[0]
            else:
                assert samples[0]["animation"] != "none", samples[0]
                assert samples[0]["duration"] == "0.7s", samples[0]
                assert samples[0]["timing"] == "linear", samples[0]
                assert samples[0]["iterations"] == "infinite", samples[0]
            probe = page.evaluate("window.__contactLoadingProbe")
            assert probe["firstModalHidden"] is False, probe
            assert probe["hiddenBeforeReady"] == 0, probe
            assert not probe["modalReplaced"] and not probe["cardReplaced"], probe
            assert not probe["contentReplacedWhilePending"], probe
            assert not probe["zeroMetricWhilePending"], probe
            assert len(document_requests) == 1
            assert page.evaluate("history.length") == history_length
            page.screenshot(
                path=tmp_path / f"contact-loading-{width}-{color_scheme}-{motion}.png"
            )
            page.evaluate("window.__contactLoadingProbe.allowFinish = true")
            controlled.release.set()
            expect(page.locator("tr[data-contact-message-id]")).to_have_count(
                50, timeout=12000
            )
            expect(modal).to_be_hidden()
            expect(page.locator("[data-contact-analysis]")).to_have_attribute(
                "data-contact-analysis-state", "ready"
            )
            assert page.evaluate("history.length") == history_length
            assert len(document_requests) == 1
            assert page.evaluate("window.__contactLoadingProbe.tableAppearances") == 1
            assert page.evaluate(
                "!document.querySelector('[data-contact-analysis]').inert "
                "&& !document.querySelector('main').inert "
                "&& !document.getElementById('cache_wait_modal').contains(document.activeElement)"
            )
            expect(
                page.get_by_role("searchbox", name="Search contact statements")
            ).to_be_enabled()
            page.wait_for_function(
                """() => {
                const active = document.querySelector('[aria-current="page"]');
                const indicator = document.querySelector('.local-store-pagination-indicator');
                if (!active || !indicator) return false;
                const target = active.getBoundingClientRect();
                const painted = indicator.getBoundingClientRect();
                return Math.abs(target.left + target.right - painted.left - painted.right) <= 2
                    && Math.abs(target.top + target.bottom - painted.top - painted.bottom) <= 2;
            }"""
            )
            fixtures._assert_analysis_summary_and_pagination_geometry(
                page, tmp_path / "ready-table-geometry.json"
            )
            assert not errors
            assert fixture.before == (
                fixture.history.read_bytes(),
                fixture.registry.read_bytes(),
            )
            (tmp_path / "loading-geometry.json").write_text(
                json.dumps({"samples": samples, "probe": probe}, indent=2) + "\n"
            )
    finally:
        controlled.release.set()
        context.close()


def test_browser_back_cancels_contact_polling_and_does_not_mutate_the_source_page(
    disposable_browser, contact_analysis_server
):
    fixture = contact_analysis_server
    controlled = ControlledContactResults(fixture)
    context = disposable_browser.new_context(
        viewport={"width": 1280, "height": 1000}, reduced_motion="reduce"
    )
    page = context.new_page()
    source_url = (
        fixture.url
        + "/browser?"
        + urlencode(
            {
                "view": "text",
                "source": "wechat",
                "account": "account:wxid_account_a",
                "chat_kind": "group",
                "session": fixture.session,
            }
        )
    )
    try:
        page.goto(source_url, wait_until="networkidle")
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        link = (
            page.locator("a[data-wechat-contact-link]")
            .filter(has_text=fixtures.VIEWER_NAME)
            .first
        )
        with patch(
            "app.core.wechat_contact_analysis.query_contact_analysis",
            side_effect=controlled,
        ):
            link.click()
            expect(page.locator("#cache_wait_modal")).to_be_visible()
            page.wait_for_function(
                "performance.getEntriesByType('resource').some(entry => "
                "entry.initiatorType === 'fetch' && entry.name.includes('/browser/wechat/contacts/'))",
                timeout=12000,
            )
            page.go_back(wait_until="networkidle")
            expect(page).to_have_url(source_url)
            expect(page.locator("[data-wechat-resources]")).to_be_visible()
            expect(page.locator("#cache_wait_modal")).to_be_hidden()
            count = len(controlled.json_requests())
            controlled.release.set()
            page.wait_for_timeout(4500)
            assert len(controlled.json_requests()) == count
            expect(page).to_have_url(source_url)
            assert page.locator("[data-contact-analysis]").count() == 0
            assert not page.evaluate("document.querySelector('main').inert")
    finally:
        controlled.release.set()
        context.close()


def test_dismissed_loading_keeps_an_unsubmitted_search_draft_and_focus(
    disposable_browser, contact_analysis_server
):
    fixture = contact_analysis_server
    controlled = ControlledContactResults(fixture)
    context = disposable_browser.new_context(
        viewport={"width": 1280, "height": 1000}, reduced_motion="reduce"
    )
    page = context.new_page()
    try:
        with patch(
            "app.core.wechat_contact_analysis.query_contact_analysis",
            side_effect=controlled,
        ):
            page.goto(_contact_url(fixture), wait_until="domcontentloaded")
            expect(page.locator("#cache_wait_modal")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#cache_wait_modal")).to_be_hidden()
            page.wait_for_function("!document.querySelector('main').inert")
            search = page.get_by_role("searchbox", name="Search contact statements")
            search.fill("An unsubmitted search draft")
            search.focus()
            scroll = page.evaluate("({x: scrollX, y: scrollY})")
            controlled.release.set()
            expect(page.locator("tr[data-contact-message-id]")).to_have_count(
                50, timeout=12000
            )
            expect(search).to_have_value("An unsubmitted search draft")
            expect(search).to_be_focused()
            assert page.evaluate("({x: scrollX, y: scrollY})") == scroll
    finally:
        controlled.release.set()
        context.close()


@pytest.mark.parametrize("failure", ["source-error", "invalid-json"])
def test_contact_loading_error_closes_the_spinner_and_offers_a_working_retry(
    disposable_browser, contact_analysis_server, failure
):
    fixture = contact_analysis_server
    controlled = ControlledContactResults(fixture)
    context = disposable_browser.new_context(
        viewport={"width": 390, "height": 1000}, reduced_motion="reduce"
    )
    page = context.new_page()
    if failure == "invalid-json":

        def fail_json(route):
            if (
                controlled.fail.is_set()
                and "application/json" in route.request.headers.get("accept", "")
            ):
                route.fulfill(
                    status=500,
                    content_type="text/plain",
                    body="Synthetic untrusted error",
                )
            else:
                route.continue_()

        page.route("**/browser/wechat/contacts/**", fail_json)
    try:
        with patch(
            "app.core.wechat_contact_analysis.query_contact_analysis",
            side_effect=controlled,
        ):
            page.goto(_contact_url(fixture), wait_until="domcontentloaded")
            expect(page.locator("#cache_wait_modal")).to_be_visible()
            controlled.fail.set()
            expect(page.locator("#cache_wait_modal")).to_be_hidden(timeout=12000)
            expect(
                page.get_by_text("Statements could not be loaded.", exact=True)
            ).to_be_visible()
            retry = page.get_by_role("link", name="Refresh", exact=True)
            expect(retry).to_be_visible()
            assert "Synthetic untrusted error" not in page.locator("body").inner_text()
            assert not page.evaluate("document.querySelector('main').inert")
            assert urlsplit(page.url).path.endswith(fixture.target_id)
            controlled.fail.clear()
            controlled.release.set()
            if failure == "invalid-json":
                page.unroute("**/browser/wechat/contacts/**", fail_json)
            retry.click()
            expect(page.locator("tr[data-contact-message-id]")).to_have_count(50)
            expect(page.locator("#cache_wait_modal")).to_be_hidden()
    finally:
        controlled.release.set()
        context.close()
