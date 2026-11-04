"""Resolve product UI copy through a bounded locale catalog.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from string import Formatter
from types import MappingProxyType
from typing import Mapping


VERSION = "v1.0.0-codex.0"
DEFAULT_UI_LOCALE = "en-US"
UI_MESSAGES = MappingProxyType(
    {
        "en-US": MappingProxyType(
            {
                "payment.kind.transfer": "Transfer",
                "payment.kind.red_packet": "Red packet",
                "payment.prefix.transfer": "[Transfer]",
                "payment.prefix.red_packet": "[Red packet]",
                "payment.currency.unknown": "Currency unknown",
                "payment.provider.transfer": "WeChat transfer",
                "payment.provider.red_packet": "WeChat red packet",
                "payment.transfer.to_you": "Transfer to you",
                "payment.transfer.to_recipient": "Transfer to {recipient}",
                "payment.transfer.received": (
                    "Transfer of {amount} received. "
                    "Upgrade WeChat to the latest version to accept it."
                ),
                "payment.transfer.from_sender": (
                    "{sender} sent you {amount}. "
                    "Upgrade WeChat to the latest version to accept it."
                ),
                "payment.red_packet.notification": (
                    "I sent you a red packet. Open it to view."
                ),
                "payment.red_packet.open": "Open red packet",
                "payment.red_packet.view": "View red packet",
                "payment.red_packet.none_remaining": "No red packets remaining",
                "payment.red_packet.opened": "Red packet opened",
                "payment.red_packet.expired": "Red packet expired",
                "payment.red_packet.none_available": "No red packets available",
                "payment.shared.amount_per_person": "Each person must pay {amount}.",
            }
        )
    }
)


def ui_locale(locale: str | None = DEFAULT_UI_LOCALE) -> str:
    """Resolve available catalog names; unsupported locales explicitly fall back."""
    if isinstance(locale, str):
        normalized = locale.replace("_", "-").lower()
        if normalized == "en":
            normalized = DEFAULT_UI_LOCALE.lower()
        for supported in UI_MESSAGES:
            if supported.replace("_", "-").lower() == normalized:
                return supported
    return DEFAULT_UI_LOCALE


def ui_message(
    message_key: str,
    *,
    locale: str | None = DEFAULT_UI_LOCALE,
    parameters: Mapping[str, str] | None = None,
) -> str:
    """Format plain text only; callers retain responsibility for HTML escaping.

    Catalog keys and exact string parameters are intentional contracts. Missing
    messages, missing arguments, extra arguments, and nonliteral values fail
    explicitly instead of silently exposing a key or invoking custom formatting.
    """
    template = UI_MESSAGES[ui_locale(locale)][message_key]
    expected = {
        name for _, name, _, _ in Formatter().parse(template) if name is not None
    }
    supplied = dict(parameters) if parameters is not None else {}
    if set(supplied) != expected:
        raise ValueError("UI message parameters do not match the catalog template.")
    if any(type(value) is not str for value in supplied.values()):
        raise ValueError("UI message parameters must be literal strings.")
    return template.format_map(supplied)
