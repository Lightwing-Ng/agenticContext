"""Payment-provider localization retains exact source proof and literal fields.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.core import ui_i18n
from app.core.wechat_payment_copy import localize_payment_presentation
from app.core.wechat_payments import payment_card_text, payment_presentation


def _source(fields, *, subtype=2000):
    metadata = {
        "schema_version": 1,
        "kind": "transfer" if subtype == 2000 else "red_packet",
        "appmsg_subtype": subtype,
        "display_fields": fields,
    }
    evidence = {
        "message_type": 49,
        "appmsg_subtype": subtype,
        "text_extraction_kind": "app_text",
        "payment_card_fields": metadata,
    }
    text = payment_card_text(metadata)
    return payment_presentation(evidence, text=text), evidence, text


def _localized(fields, *, subtype=2000, locale="en-US"):
    payment, evidence, text = _source(fields, subtype=subtype)
    return localize_payment_presentation(payment, evidence, text, locale=locale)


@pytest.mark.parametrize("locale", [None, "", "en", "EN-us", "en_US", "zh-CN", "xx"])
def test_available_english_catalog_and_explicit_locale_fallback(locale):
    assert ui_i18n.ui_locale(locale) == "en-US"
    assert ui_i18n.ui_message("payment.kind.transfer", locale=locale) == "Transfer"


def test_second_catalog_is_resolved_without_parser_or_template_changes(monkeypatch):
    french = dict(ui_i18n.UI_MESSAGES["en-US"])
    french.update(
        {
            "payment.kind.transfer": "Virement",
            "payment.prefix.transfer": "[Virement]",
            "payment.provider.transfer": "Virement WeChat",
        }
    )
    monkeypatch.setattr(
        ui_i18n, "UI_MESSAGES", {**ui_i18n.UI_MESSAGES, "fr-FR": french}
    )
    assert ui_i18n.ui_locale("FR_fr") == "fr-FR"
    result = _localized({"title": "微信转账"}, locale="FR_fr")
    assert result["label"] == "Virement"
    assert result["display_text"] == "[Virement]\nVirement WeChat"
    assert {part["lang"] for part in result["text_parts"]} == {"fr-FR"}


def test_catalog_keys_and_parameters_fail_explicitly_and_remain_plain_text():
    with pytest.raises(KeyError):
        ui_i18n.ui_message("payment.does_not_exist")
    for parameters in ({}, {"recipient": "a", "unexpected": "b"}, {"recipient": 5}):
        with pytest.raises(ValueError):
            ui_i18n.ui_message("payment.transfer.to_recipient", parameters=parameters)
    hostile = "<script>run()</script>&{amount}"
    assert (
        ui_i18n.ui_message(
            "payment.transfer.to_recipient", parameters={"recipient": hostile}
        )
        == "Transfer to " + hostile
    )


@pytest.mark.parametrize("brand", ["", "微信", "WeChat", "Weixin"])
@pytest.mark.parametrize("word", ["转账", "转帐", "轉賬", "轉帳"])
@pytest.mark.parametrize("path", ["title", "des"])
def test_whole_provider_transfer_aliases_share_one_catalog_key(brand, word, path):
    result = _localized({path: brand + word})
    assert result["display_text"] == "[Transfer]\nWeChat transfer"
    assert result["text_parts"][1]["message_key"] == "payment.provider.transfer"


@pytest.mark.parametrize("literal", ["给你转账", "给你转帐", "給你轉賬", "給你轉帳"])
def test_transfer_to_you_aliases(literal):
    assert _localized({"title": literal})["display_text"] == (
        "[Transfer]\nTransfer to you"
    )


@pytest.mark.parametrize("brand", ["", "微信", "WeChat", "Weixin"])
@pytest.mark.parametrize("word", ["红包", "紅包", "利是"])
def test_red_packet_provider_aliases(brand, word):
    assert _localized({"title": brand + word}, subtype=2001)["display_text"] == (
        "[Red packet]\nWeChat red packet"
    )


@pytest.mark.parametrize(
    "literal,expected",
    [
        ("领取红包", "Open red packet"),
        ("領取紅包", "Open red packet"),
        ("領取利是", "Open red packet"),
        ("拆红包", "Open red packet"),
        ("打开红包", "Open red packet"),
        ("打開紅包", "Open red packet"),
        ("查看红包", "View red packet"),
        ("查看紅包", "View red packet"),
        ("點擊查看", "View red packet"),
        ("点击查看", "View red packet"),
        ("Details", "View red packet"),
        ("红包已被领完", "No red packets remaining"),
        ("紅包已被領完", "No red packets remaining"),
        ("利是已被領完", "No red packets remaining"),
        ("红包已领取", "Red packet opened"),
        ("紅包已領取", "Red packet opened"),
        ("利是已領取", "Red packet opened"),
    ],
)
def test_status_aliases_are_limited_to_provider_status_fields(literal, expected):
    result = _localized({"wcpayinfo/receiverdes": literal}, subtype=2001)
    assert result["display_text"] == "[Red packet]\n" + expected
    assert _localized({"wcpayinfo/pay_memo": literal}, subtype=2001)[
        "display_text"
    ] == ("[Red packet]\n" + literal)


@pytest.mark.parametrize(
    "literal,expected",
    [
        ("Red Packet expired", "Red packet expired"),
        ("None available", "No red packets available"),
    ],
)
def test_narrow_party_status_titles_use_catalog(literal, expected):
    result = _localized({"wcpayinfo/sendertitle": literal}, subtype=2001)
    assert result["display_text"] == "[Red packet]\n" + expected


@pytest.mark.parametrize(
    "path,literal",
    [
        ("wcpayinfo/pay_memo", "给你转账\n轉賬\n轉帳"),
        ("wcpayinfo/redenvelopedesc", "微信红包\nCustom greeting"),
        ("wcpayinfo/receivertitle", "恭喜发财，大吉大利"),
        ("wcpayinfo/sendertitle", "恭喜發財，大吉大利！"),
        ("title", "不要给你转账"),
        ("des", "轉賬\nI wrote this"),
        ("des", " 微信转账 "),
        ("wcpayinfo/feedesc", "(null)"),
    ],
)
def test_freeform_fields_and_unrecognized_whole_fields_remain_literal(path, literal):
    result = _localized({path: literal}, subtype=2001)
    assert result["display_text"] == "[Red packet]\n" + literal
    assert result["text_parts"][1] == {"text": literal, "lang": ""}


def test_original_duplicate_handling_and_translated_provider_dedup_are_separate():
    result = _localized(
        {
            "title": "轉賬",
            "des": "微信轉帳",
            "wcpayinfo/pay_memo": "WeChat transfer",
            "wcpayinfo/receivertitle": "WeChat transfer",
        }
    )
    assert result["display_text"] == "[Transfer]\nWeChat transfer\nWeChat transfer"
    assert result["text_parts"][1]["message_key"] == "payment.provider.transfer"
    assert result["text_parts"][2] == {"text": "WeChat transfer", "lang": ""}


def test_default_red_packet_description_and_greeting_are_separate_parts():
    result = _localized(
        {"des": "我给你发了一个红包，赶紧去拆! 祝：恭喜发财，大吉大利！"},
        subtype=2001,
    )
    assert result["display_text"] == (
        "[Red packet]\nI sent you a red packet. Open it to view.\n恭喜发财，大吉大利！"
    )
    assert result["text_parts"][-1] == {"text": "恭喜发财，大吉大利！", "lang": ""}


@pytest.mark.parametrize(
    "literal,expected,parameters",
    [
        (
            "收到转账0005.20元。如需收钱，请点此升级至最新版本",
            "Transfer of 0005.20元 received. Upgrade WeChat to the latest version to accept it.",
            {"amount": "0005.20元"},
        ),
        (
            "Source sender向你转账￥5.20。如需收钱，请点击升级至最新版本",
            "Source sender sent you ￥5.20. Upgrade WeChat to the latest version to accept it.",
            {"sender": "Source sender", "amount": "￥5.20"},
        ),
        (
            "Source sender向你轉帳1,234.50元。如需收錢，請點此升級至最新版本",
            "Source sender sent you 1,234.50元. Upgrade WeChat to the latest version to accept it.",
            {"sender": "Source sender", "amount": "1,234.50元"},
        ),
    ],
)
def test_complete_transfer_notifications_preserve_raw_amount_units(
    literal, expected, parameters
):
    result = _localized({"des": literal})
    assert result["display_text"] == "[Transfer]\n" + expected
    assert result["text_parts"][1]["parameters"] == parameters
    assert result["currency"] == ""
    assert result["currency_label"] == "Currency unknown"


@pytest.mark.parametrize(
    "literal",
    [
        "每人需支付0005.20元",
        "每人需支付￥5.20",
        "每人需支付1,234.50元",
    ],
)
def test_complete_aa_request_retains_amount_and_does_not_infer_currency(literal):
    result = _localized({"wcpayinfo/senderdes": literal}, subtype=2001)
    amount = literal.removeprefix("每人需支付")
    assert (
        result["display_text"] == "[Red packet]\nEach person must pay " + amount + "."
    )
    assert result["currency"] == ""


@pytest.mark.parametrize(
    "path,literal",
    [
        ("des", "收到转账12元。如需收钱，请点此升级至最新版本 EXTRA"),
        ("des", "prefix 收到转账12元。如需收钱，请点此升级至最新版本"),
        ("des", "收到转账12元。如需收钱，请点此升级至最新版本\n"),
        ("des", "我给你发了一个红包，赶紧去拆! EXTRA"),
        ("wcpayinfo/senderdes", "每人需支付12元以后再说"),
        ("wcpayinfo/receiverdes", "领取红包以获得奖励"),
    ],
)
def test_provider_patterns_require_full_field_match(path, literal):
    subtype = 2000 if "转账" in literal else 2001
    result = _localized({path: literal}, subtype=subtype)
    assert result["text_parts"][-1] == {"text": literal, "lang": ""}


def test_hostile_participant_is_literal_parameter_not_markup_or_template_code():
    sender = "<img src=x onerror=run()> {amount}"
    literal = sender + "向你转账12元。如需收钱，请点此升级至最新版本"
    result = _localized({"des": literal})
    part = result["text_parts"][1]
    assert part["parameters"] == {"sender": sender, "amount": "12元"}
    assert part["text"].startswith(sender + " sent you 12元.")


@pytest.mark.parametrize("mutation", ["absent", "body", "schema", "kind", "subtype"])
def test_missing_or_inconsistent_metadata_keeps_body_literal(mutation):
    payment, evidence, text = _source({"title": "给你转账"})
    if mutation == "absent":
        evidence.pop("payment_card_fields")
    elif mutation == "body":
        text += "\nUnexpected suffix"
    elif mutation == "schema":
        evidence["payment_card_fields"]["schema_version"] = 2
    elif mutation == "kind":
        payment["kind"] = "red_packet"
    else:
        evidence["appmsg_subtype"] = 2001
    result = localize_payment_presentation(payment, evidence, text)
    assert result["display_text"] == text
    assert result["text_parts"] == [{"text": text, "lang": ""}]


def test_regional_transfer_uses_proven_kind_prefix_and_preserves_original_observation():
    payment, evidence, text = _source(
        {
            "title": "给你转账",
            "des": "WeChat转账",
            "wcpayinfo/receivertitle": "Transfer to you",
            "wcpayinfo/sendertitle": "Transfer to Fixture recipient",
            "wcpayinfo/senderdes": "HK$0.19",
            "wcpayinfo/receiverdes": "HK$0.19",
        },
        subtype=2001,
    )
    before = json.dumps([payment, evidence, text], ensure_ascii=False, sort_keys=True)
    result = localize_payment_presentation(payment, evidence, text)
    assert result["display_text"] == (
        "[Transfer]\nTransfer to you\nWeChat transfer\nTransfer to Fixture recipient\nHK$0.19"
    )
    assert result["currency"] == result["currency_label"] == "HKD"
    assert result["source_kind"] == "red_packet"
    assert (
        json.dumps([payment, evidence, text], ensure_ascii=False, sort_keys=True)
        == before
    )
    result["currency_evidence"][0]["marker"] = "changed output"
    assert (
        json.dumps([payment, evidence, text], ensure_ascii=False, sort_keys=True)
        == before
    )


def test_contradictory_provider_title_stays_literal_and_does_not_reclassify():
    result = _localized({"title": "微信红包"})
    assert result["kind"] == "transfer"
    assert result["display_text"] == "[Transfer]\n微信红包"
    assert result["text_parts"][-1]["lang"] == ""


def test_fresh_copy_does_not_mutate_source_freeform_values_or_projection():
    payment, evidence, text = _source(
        {
            "title": "给你转账",
            "wcpayinfo/pay_memo": "  literal\n\n  memo  ",
            "wcpayinfo/redenvelopedesc": "\tSource greeting\n",
        }
    )
    before = deepcopy((payment, evidence, text))
    result = localize_payment_presentation(payment, evidence, text)
    assert (payment, evidence, text) == before
    assert result is not payment
    assert result["display_text"].endswith("  literal\n\n  memo  \n\tSource greeting\n")
