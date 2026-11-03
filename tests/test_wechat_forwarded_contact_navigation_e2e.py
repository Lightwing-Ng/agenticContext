"""Forwarded contact sources reveal only the required transcript ancestors.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from threading import Thread

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from tests import test_sidebar_e2e as sidebar_fixtures
from tests import test_wechat_contact_analysis_routes as route_fixtures
from tests.test_wechat_forwarded_contact_analysis import CID, NESTED


disposable_browser = sidebar_fixtures.disposable_browser
forwarded_activity_store = route_fixtures.forwarded_activity_store
forwarded_contact_app = route_fixtures.forwarded_contact_app


@pytest.fixture
def forwarded_contact_server(forwarded_contact_app):
    app, fixture = forwarded_contact_app
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [823, 390])
def test_nested_contact_source_and_return_preserve_disclosure_scope(
    disposable_browser, forwarded_contact_server, width
):
    base, fixture = forwarded_contact_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1100},
        reduced_motion="reduce",
    )
    page = context.new_page()
    try:
        page.goto(
            base + "/browser/wechat/contacts/" + CID + "?q=TARGET-NESTED",
            wait_until="networkidle",
        )
        row = page.locator("tr[data-contact-statement-id]")
        expect(row).to_have_count(1)
        expect(row).to_contain_text(NESTED)
        expect(row).to_contain_text("Forwarded message")
        parent = fixture["request"]["id"]
        source_url = "/browser/wechat/messages/" + parent + "_forwarded_3_forwarded_1"
        body_source = row.locator("a[data-wechat-contact-statement]")
        conversation_source = row.locator("td").nth(3).get_by_role("link")
        expect(body_source).to_have_text(NESTED)
        expect(body_source).to_have_attribute("href", source_url)
        expect(conversation_source).to_have_attribute("href", source_url)
        body_source.click()
        target_id = "wechat_message_" + parent + "_forwarded_3_forwarded_1"
        target = page.locator("#" + target_id)
        expect(target).to_be_visible()
        assert page.url.endswith("#" + target_id)
        assert target.evaluate(
            """(target) => {
                let details = target.parentElement.closest('details[data-wechat-forwarded]');
                let count = 0;
                while (details) {
                    if (!details.open) return false;
                    count += 1;
                    details = details.parentElement.closest('details[data-wechat-forwarded]');
                }
                return count === 2;
            }"""
        )
        own_forwarded = target.locator(":scope > details[data-wechat-forwarded]")
        expect(own_forwarded).not_to_have_attribute("open", "")
        assert page.locator("details.wechat-provenance[open]").count() == 0
        time = page.locator("#wechat_time_" + parent + "_forwarded_3_forwarded_1")
        expect(time).to_have_text("2 Jan 2020 03:04:05")
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )

        # Returning from a child contact must not open every sibling's provenance.
        target.locator(":scope > details.wechat-provenance > summary").press("Enter")
        expect(target.locator(":scope > details.wechat-provenance")).to_have_attribute(
            "open", ""
        )
        target.locator("[data-wechat-contact-link]").first.click()
        expect(page.locator("[data-contact-analysis]")).to_be_visible()
        page.go_back(wait_until="networkidle")
        target = page.locator("#" + target_id)
        expect(target).to_be_visible()
        expect(target.locator(":scope > details.wechat-provenance")).to_have_attribute(
            "open", ""
        )
        assert page.locator("details.wechat-provenance[open]").count() == 1
        expect(
            target.locator(":scope > details[data-wechat-forwarded]")
        ).not_to_have_attribute("open", "")

        # The explicit source link starts a fresh view with only its source tree open.
        target.locator("[data-wechat-contact-link]").first.click()
        expect(page.locator("[data-contact-analysis]")).to_be_visible()
        page.locator("[data-wechat-contact-back]").click()
        expect(page.locator("#" + target_id)).to_be_visible()
        assert page.locator("details.wechat-provenance[open]").count() == 0

        # A same-document source fragment must also reveal and position its target.
        first_id = "wechat_message_" + parent + "_forwarded_1"
        page.goto(page.url.split("#", 1)[0] + "#" + first_id)
        expect(page.locator("#" + first_id)).to_be_in_viewport()
    finally:
        context.close()
