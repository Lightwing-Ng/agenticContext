"""Localized payment cards share bubble geometry without activating payment links.

Code version: v1.2.2-codex.0
"""

from __future__ import annotations

import csv
from io import StringIO
import json
import os
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from tests import test_sidebar_e2e
from tests import test_wechat_payment_presentation as payment_cases
from tests.test_wechat_payment_presentation import payment_fixture


disposable_browser = test_sidebar_e2e.disposable_browser
PAYMENT_SYMBOLS = {
    ("transfer", "CNY"): "yensign.arrow.trianglehead.counterclockwise.rotate.90.svg",
    ("red_packet", "CNY"): "yensign.circle.fill.svg",
    ("transfer", "HKD"): "dollarsign.arrow.trianglehead.counterclockwise.rotate.90.svg",
    ("red_packet", "HKD"): "dollarsign.circle.fill.svg",
    ("transfer", ""): "arrow.trianglehead.2.clockwise.svg",
    ("red_packet", ""): "gift.fill.svg",
}


@pytest.fixture(scope="module")
def payment_server(tmp_path_factory):
    fixture = payment_fixture(tmp_path_factory.mktemp("wechat-payment-presentation"))
    server = make_server("127.0.0.1", 0, fixture.app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_payment_cards_reuse_literal_bubbles_for_all_native_roles(
    disposable_browser, payment_server, width, color_scheme, tmp_path
):
    fixture = payment_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    external_requests = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "request",
        lambda request: (
            external_requests.append(request.url)
            if not request.url.startswith(fixture.url)
            else None
        ),
    )
    try:
        page.goto(
            fixture.url
            + "/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "account": "account:" + fixture.primary["source_account_id"],
                    "session": fixture.session,
                }
            ),
            wait_until="domcontentloaded",
        )
        disclosures = page.locator("details.wechat-forwarded")
        expect(disclosures).to_have_count(2)
        page.evaluate("() => document.fonts.ready")
        sidebar = page.locator("#sidebar_toggle")
        if sidebar.get_attribute("aria-expanded") == "true":
            sidebar.click()
        for disclosure in disclosures.all():
            if disclosure.get_attribute("open") is None:
                disclosure.locator(":scope > summary").click()
        cards = page.locator(
            ".wechat-payment[data-wechat-payment-kind][data-wechat-payment-currency]"
        )
        expect(cards).to_have_count(7)
        for card in cards.all():
            expect(card).to_be_visible()
            expect(card.locator(".wechat-payment-icon")).to_have_count(1)
            expect(
                card.locator("div.wechat-message-text.wechat-payment-text")
            ).to_have_count(1)
            expect(
                card.locator("a, button, img, iframe, video, audio, script")
            ).to_have_count(0)
            label = card.get_attribute("aria-label")
            assert label
            currency = card.get_attribute("data-wechat-payment-currency")
            if currency:
                assert label.startswith(currency + " ")
            else:
                assert "CNY" not in label and "HKD" not in label
        primary = page.locator("#wechat_message_" + fixture.identifiers["cny-sent"])
        received = page.locator(
            "#wechat_message_" + fixture.identifiers["hkd-received"]
        )
        quoted = page.locator("#wechat_message_" + fixture.identifiers["quoted"])
        assert (
            primary.locator(".wechat-payment-text").text_content()
            == (fixture.parsed["cny-sent"]["text"])
        )
        assert (
            received.locator(".wechat-payment-text").text_content()
            == (fixture.parsed["hkd-received"]["text"])
        )
        expect(quoted.locator("blockquote .wechat-payment")).to_have_count(1)
        expect(
            page.locator("[data-wechat-embedded-row] .wechat-payment")
        ).to_have_count(2)
        for key in ("prose", "other-app-subtype"):
            expect(
                page.locator("#wechat_message_" + fixture.identifiers[key]).locator(
                    ".wechat-payment"
                )
            ).to_have_count(0)
        assert page.evaluate("() => window.paymentScript === undefined")
        geometry = cards.evaluate_all(
            """cards => cards.map(card => {
                const bounds = card.getBoundingClientRect();
                const bubble = card.closest('article').getBoundingClientRect();
                const icon = card.querySelector('.wechat-payment-icon');
                const text = card.querySelector('.wechat-payment-text');
                const textStyle = getComputedStyle(text);
                const iconStyle = getComputedStyle(icon);
                return {
                    currency: card.dataset.wechatPaymentCurrency,
                    kind: card.dataset.wechatPaymentKind,
                    width: bounds.width,
                    left: bounds.left,
                    right: bounds.right,
                    bubbleLeft: bubble.left,
                    bubbleRight: bubble.right,
                    iconWidth: icon.getBoundingClientRect().width,
                    iconHeight: icon.getBoundingClientRect().height,
                    iconBackground: iconStyle.backgroundImage,
                    iconSymbol: iconStyle.getPropertyValue('--wechat-resource-symbol'),
                    textWhiteSpace: textStyle.whiteSpace,
                    textFontSize: textStyle.fontSize,
                    overflow: text.scrollWidth - text.clientWidth,
                };
            })"""
        )
        for item in geometry:
            assert item["width"] > 0
            assert item["left"] >= item["bubbleLeft"] - 1
            assert item["right"] <= item["bubbleRight"] + 1
            assert item["iconWidth"] == item["iconHeight"] == 32
            symbol = PAYMENT_SYMBOLS[(item["kind"], item["currency"])]
            assert f"/static/images/{symbol}" in item["iconSymbol"]
            assert item["textWhiteSpace"] == "pre-wrap"
            assert item["textFontSize"] == "15px"
            assert item["overflow"] <= 1
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= innerWidth + 1"
        )
        assert not errors
        assert not external_requests
        assert fixture.history.read_bytes() == fixture.preserved
        artifacts = Path(
            os.environ.get(
                "WECHAT_PAYMENT_E2E_ARTIFACTS",
                str(tmp_path / "browser"),
            )
        )
        artifacts.mkdir(parents=True, exist_ok=True)
        name = f"{color_scheme}-{width}"
        (artifacts / f"{name}.json").write_text(
            json.dumps(geometry, indent=2) + "\n", encoding="utf-8"
        )
        primary.scroll_into_view_if_needed()
        page.screenshot(path=artifacts / f"{name}.png")
    finally:
        context.close()


def test_regional_provider_transfer_corrects_only_presentation_and_preserves_raw_csv(
    disposable_browser, tmp_path, monkeypatch
):
    original_factory = payment_cases.payment_xml
    provider_xml = (
        "<msg><appmsg><type>2001</type><title>给你转账</title>"
        "<des>WeChat转账</des><wcpayinfo>"
        "<receivertitle>Transfer to you</receivertitle>"
        "<sendertitle>Transfer to Fixture recipient</sendertitle>"
        "<senderdes>HK$0.19</senderdes><receiverdes>HK$0.19</receiverdes>"
        "</wcpayinfo></appmsg></msg>"
    )

    def regional_card(subtype, amount, **kwargs):
        if subtype == 2001 and amount == "HK$ 88.00":
            return provider_xml
        return original_factory(subtype, amount, **kwargs)

    monkeypatch.setattr(payment_cases, "payment_xml", regional_card)
    fixture = payment_fixture(tmp_path)
    key = "hkd-received"
    original = fixture.parsed[key]
    assert original["text"].startswith("[Red packet]\n")
    assert original["payment_card_fields"]["kind"] == "red_packet"
    result = payment_cases.payment_page(fixture)
    message = next(
        item for item in result.items if item.stable_id == fixture.identifiers[key]
    )
    payment = message.messaging["payment"]
    assert (payment["kind"], payment["currency"], payment["source_kind"]) == (
        "transfer",
        "HKD",
        "red_packet",
    )
    assert payment["kind_evidence"] == [
        "title",
        "des",
        "wcpayinfo/receivertitle",
        "wcpayinfo/sendertitle",
    ]
    assert message.content_text == original["text"]
    assert (
        message.messaging["provenance"]["payment_card_fields"]
        == (original["payment_card_fields"])
    )
    assert message.messaging["account_id"] == fixture.primary["source_account_id"]
    server = make_server("127.0.0.1", 0, fixture.app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = disposable_browser.new_context(reduced_motion="reduce")
    try:
        page = context.new_page()
        page.goto(
            f"http://127.0.0.1:{server.server_port}/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "account": "account:" + fixture.primary["source_account_id"],
                    "session": fixture.session,
                }
            ),
            wait_until="domcontentloaded",
        )
        card = page.locator("#wechat_message_" + fixture.identifiers[key]).locator(
            ".wechat-payment"
        )
        expect(card).to_have_attribute("data-wechat-payment-kind", "transfer")
        expect(card).to_have_attribute("data-wechat-payment-currency", "HKD")
        expect(card).to_have_attribute("aria-label", "HKD Transfer")
        visible_text = card.locator(".wechat-payment-text").text_content()
        assert visible_text == "\n".join(
            [
                "[Transfer]",
                "Transfer to you",
                "WeChat transfer",
                "Transfer to Fixture recipient",
                "HK$0.19",
            ]
        )
        assert "[Red packet]" not in visible_text
        assert "给你转账" not in visible_text and "WeChat转账" not in visible_text
        icon = card.locator(".wechat-payment-icon").evaluate(
            "element => getComputedStyle(element).getPropertyValue('--wechat-resource-symbol')"
        )
        assert PAYMENT_SYMBOLS[("transfer", "HKD")] in icon
        response = fixture.app.test_client().get(
            f"/browser/wechat/session/{fixture.session}/export.csv",
            query_string={"account": "account:" + fixture.primary["source_account_id"]},
        )
        assert response.status_code == 200
        exported = list(csv.DictReader(StringIO(response.data.decode("utf-8-sig"))))
        assert sum(item["Statement"] == original["text"] for item in exported) == 1
        assert fixture.history.read_bytes() == fixture.preserved
        assert original["payment_card_fields"]["kind"] == "red_packet"
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
def test_localized_payment_copy_keeps_literal_notes_and_embedded_roles(
    disposable_browser, tmp_path, width
):
    fixture = payment_cases.localized_payment_fixture(tmp_path)
    server = make_server("127.0.0.1", 0, fixture.app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000}, reduced_motion="reduce"
    )
    try:
        page = context.new_page()
        page.goto(
            f"http://127.0.0.1:{server.server_port}/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "account": "account:" + fixture.primary["source_account_id"],
                    "session": fixture.session,
                }
            ),
            wait_until="domcontentloaded",
        )
        disclosures = page.locator("details.wechat-forwarded")
        expect(disclosures).to_have_count(2)
        page.evaluate("() => document.fonts.ready")
        sidebar = page.locator("#sidebar_toggle")
        if sidebar.get_attribute("aria-expanded") == "true":
            sidebar.click()
        for disclosure in disclosures.all():
            if disclosure.get_attribute("open") is None:
                disclosure.locator(":scope > summary").click()
        cards = page.locator(".wechat-payment")
        expect(cards).to_have_count(7)
        for card in cards.all():
            expect(card).to_be_visible()
            expect(card.locator("a, button, img, script, iframe")).to_have_count(0)
            assert card.locator(".wechat-payment-text").get_attribute("lang") != "zh-CN"
        transfer = page.locator(
            "#wechat_message_" + fixture.identifiers["cny-sent"]
        ).locator(".wechat-payment-text")
        assert transfer.text_content() == "\n".join(
            [
                "[Transfer]",
                "Transfer to you",
                "WeChat transfer",
                "CNY 1,234.50",
                fixture.literal_memo,
            ]
        )
        red_packet = page.locator(
            "#wechat_message_" + fixture.identifiers["hkd-received"]
        ).locator(".wechat-payment-text")
        assert red_packet.text_content() == "\n".join(
            [
                "[Red packet]",
                "WeChat red packet",
                "HK$ 88.00",
                "Original source copy",
                fixture.literal_greeting,
            ]
        )
        translated = page.locator(".wechat-payment-text span[lang='en-US']")
        labels = translated.all_text_contents()
        assert "Transfer to you" in labels
        assert "WeChat transfer" in labels
        assert "WeChat red packet" in labels
        assert all(
            "转" not in label and "轉" not in label and "紅" not in label
            for label in labels
        )
        quoted = page.locator("#wechat_message_" + fixture.identifiers["quoted"])
        assert quoted.locator("blockquote .wechat-payment-text").text_content() == (
            "[Red packet]\nWeChat red packet\nHKD 16.00\nOriginal source copy"
        )
        forwarded = page.locator("[data-wechat-embedded-row] .wechat-payment-text")
        assert set(forwarded.all_text_contents()) == {
            "[Red packet]\nWeChat red packet\nCNY 12.50\nOriginal source copy",
            "[Transfer]\nWeChat transfer\nHK$ 88.00\nOriginal source copy",
        }
        unknown = page.locator(
            "#wechat_message_" + fixture.identifiers["unknown-received"]
        ).locator(".wechat-payment-text")
        assert unknown.text_content() == fixture.parsed["unknown-received"]["text"]
        prose = page.locator("#wechat_message_" + fixture.identifiers["prose"])
        expect(prose.locator(".wechat-payment")).to_have_count(0)
        assert (
            prose.locator(":scope > .wechat-message-text").text_content()
            == (fixture.parsed["prose"]["text"])
        )
        assert page.evaluate("() => window.paymentScript === undefined")
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= innerWidth + 1"
        )
        assert fixture.history.read_bytes() == fixture.preserved
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
