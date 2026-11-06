"""Localize proven payment-provider copy while preserving source observations.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .ui_i18n import DEFAULT_UI_LOCALE, ui_locale, ui_message
from .wechat_payments import (
    payment_card_text,
    payment_presentation,
    validate_payment_card_fields,
)


VERSION = "v1.0.0-codex.0"
_KINDS = {"transfer", "red_packet"}
_SOURCE_PREFIXES = {"transfer": "[Transfer]", "red_packet": "[Red packet]"}
_TRANSFER_WORDS = ("转账", "转帐", "轉賬", "轉帳")
_PROVIDER_ALIASES = {
    **dict.fromkeys(
        tuple(
            brand + word
            for brand in ("", "微信", "WeChat", "Weixin")
            for word in _TRANSFER_WORDS
        )
        + ("WeChat transfer", "Weixin transfer"),
        "payment.provider.transfer",
    ),
    **dict.fromkeys(
        tuple(prefix + word for prefix in ("给你", "給你") for word in _TRANSFER_WORDS)
        + ("Transfer to you",),
        "payment.transfer.to_you",
    ),
    **dict.fromkeys(
        tuple(
            brand + word
            for brand in ("", "微信", "WeChat", "Weixin")
            for word in ("红包", "紅包", "利是")
        ),
        "payment.provider.red_packet",
    ),
}
_STATUS_ALIASES = {
    **dict.fromkeys(
        (
            "领取红包",
            "領取紅包",
            "领取利是",
            "領取利是",
            "拆红包",
            "拆紅包",
            "打开红包",
            "打開紅包",
        ),
        "payment.red_packet.open",
    ),
    **dict.fromkeys(
        ("查看红包", "查看紅包", "點擊查看", "点击查看", "Details"),
        "payment.red_packet.view",
    ),
    **dict.fromkeys(
        ("利是已被領完", "红包已被领完", "紅包已被領完", "红包已领完", "紅包已領完"),
        "payment.red_packet.none_remaining",
    ),
    **dict.fromkeys(
        ("利是已領取", "利是已领取", "红包已领取", "紅包已領取"),
        "payment.red_packet.opened",
    ),
}
_PARTY_TITLE_ALIASES = {
    "Red Packet expired": "payment.red_packet.expired",
    "红包已过期": "payment.red_packet.expired",
    "紅包已過期": "payment.red_packet.expired",
    "None available": "payment.red_packet.none_available",
}
_NUMBER = r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?"
_AMOUNT = r"(?P<amount>(?:[¥￥]\s*)?" + _NUMBER + r"(?:\s*元)?)"
_UPGRADE = (
    r"(?:如需收钱，请(?:点此|点击)升级至最新版本|"
    r"如需收錢，請(?:點此|點擊)升級至最新版本)[。.]?"
)
_RECEIVED_TRANSFER = re.compile(
    r"收到(?:转账|转帐|轉賬|轉帳)" + _AMOUNT + r"。" + _UPGRADE
)
_SENDER_TRANSFER = re.compile(
    r"(?P<sender>[^\r\n]+?)向你(?:转账|转帐|轉賬|轉帳)" + _AMOUNT + r"。" + _UPGRADE
)
_AMOUNT_PER_PERSON = re.compile(r"每人需支付" + _AMOUNT)
_RED_PACKET_NOTIFICATION = re.compile(
    r"(?:我给你发了一个红包，赶紧去拆|我給你發了一個紅包，趕緊去拆)[!！]"
    r"(?: ?祝[:：](?P<greeting>[\s\S]+))?"
)
_RECIPIENT_TITLE = re.compile(r"Transfer to (?P<recipient>[^\r\n]+)")


def _localized_part(message_key, locale, parameters=None):
    arguments = dict(parameters or {})
    return {
        "text": ui_message(message_key, locale=locale, parameters=arguments),
        "lang": ui_locale(locale),
        "message_key": message_key,
        "parameters": arguments,
    }


def _literal_part(text):
    return {"text": text, "lang": ""}


def _field_parts(path, literal, kind, locale):
    """Match complete provider fields, never search or replace arbitrary prose."""
    if path in {"title", "des"}:
        key = _PROVIDER_ALIASES.get(literal)
        if key is not None and (
            (kind == "transfer" and key != "payment.provider.red_packet")
            or (kind == "red_packet" and key == "payment.provider.red_packet")
        ):
            return [_localized_part(key, locale)]
    if path in {"wcpayinfo/receiverdes", "wcpayinfo/senderdes"}:
        key = _STATUS_ALIASES.get(literal)
        if key is not None and kind == "red_packet":
            return [_localized_part(key, locale)]
        match = _AMOUNT_PER_PERSON.fullmatch(literal)
        if match is not None:
            return [
                _localized_part(
                    "payment.shared.amount_per_person", locale, match.groupdict()
                )
            ]
    if path in {"wcpayinfo/receivertitle", "wcpayinfo/sendertitle"}:
        key = _PARTY_TITLE_ALIASES.get(literal)
        if key is not None and kind == "red_packet":
            return [_localized_part(key, locale)]
        if kind == "transfer":
            if literal == "Transfer to you":
                return [_localized_part("payment.transfer.to_you", locale)]
            match = _RECIPIENT_TITLE.fullmatch(literal)
            if match is not None and match["recipient"].strip():
                return [
                    _localized_part(
                        "payment.transfer.to_recipient", locale, match.groupdict()
                    )
                ]
    if path == "des" and kind == "transfer":
        for pattern, key in (
            (_RECEIVED_TRANSFER, "payment.transfer.received"),
            (_SENDER_TRANSFER, "payment.transfer.from_sender"),
        ):
            match = pattern.fullmatch(literal)
            if match is not None:
                return [_localized_part(key, locale, match.groupdict())]
    if path == "des" and kind == "red_packet":
        match = _RED_PACKET_NOTIFICATION.fullmatch(literal)
        if match is not None:
            parts = [_localized_part("payment.red_packet.notification", locale)]
            if match["greeting"] is not None:
                parts.append(_literal_part(match["greeting"]))
            return parts
    return [_literal_part(literal)]


def localize_payment_presentation(
    payment: dict[str, Any],
    evidence: Any,
    text: str,
    *,
    locale: str | None = DEFAULT_UI_LOCALE,
) -> dict[str, Any]:
    """Return a UI-only copy; exact schema 1 source proof gates field localization.

    Amount arguments preserve their complete source unit or symbol, including
    ambiguous ``元`` and yen signs. They establish no new currency, settlement,
    claim, participant identity, or account relationship. Existing original-field
    deduplication is retained; only translated provider fragments additionally
    deduplicate after localization. A literal memo or greeting never disappears
    merely because it happens to match the translated UI text.
    """
    if not isinstance(payment, dict) or not isinstance(text, str):
        raise TypeError("Payment presentation and source text have invalid types.")
    result = deepcopy(payment)
    kind = payment.get("kind")
    if isinstance(kind, str) and kind in _KINDS:
        result["label"] = ui_message("payment.kind." + kind, locale=locale)
    currency = payment.get("currency")
    result["currency_label"] = (
        currency
        if isinstance(currency, str) and currency in {"CNY", "HKD"}
        else ui_message("payment.currency.unknown", locale=locale)
    )
    parts = [_literal_part(text)] if text else []
    result.update(display_text=text, text_parts=parts)
    if (
        not isinstance(kind, str)
        or kind not in _KINDS
        or not isinstance(evidence, dict)
    ):
        return result
    try:
        metadata = validate_payment_card_fields(evidence.get("payment_card_fields"))
        if text != payment_card_text(metadata):
            return result
        proven = payment_presentation(evidence, text=text)
        if proven is None or proven["kind"] != kind:
            return result
    except (TypeError, ValueError):
        return result

    parts = [_localized_part("payment.prefix." + kind, locale)]
    source_seen = {_SOURCE_PREFIXES[metadata["kind"]]}
    translated_seen = {parts[0]["text"]}
    for path, literal in metadata["display_fields"].items():
        if not literal.strip() or literal in source_seen:
            continue
        source_seen.add(literal)
        for part in _field_parts(path, literal, kind, locale):
            if "message_key" in part:
                if part["text"] in translated_seen:
                    continue
                translated_seen.add(part["text"])
            parts.append(part)
    result.update(
        display_text="\n".join(part["text"] for part in parts),
        text_parts=parts,
    )
    return result
