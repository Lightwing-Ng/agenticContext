"""Retain literal native payment-card copy without asserting a transaction outcome.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree


VERSION = "v1.1.1-codex.0"
PAYMENT_SCHEMA_VERSION = 1
MAX_PAYMENT_FIELD_BYTES = 8192
MAX_PAYMENT_FIELDS_BYTES = 64 * 1024
MAX_PAYMENT_XML_BYTES = 256 * 1024
MAX_PAYMENT_XML_NODES = 4096
MAX_PAYMENT_XML_DEPTH = 16
_TYPE = re.compile(r"[0-9]{1,10}")
_PAYMENT_KINDS = {2000: "transfer", 2001: "red_packet"}
_PAYMENT_LABELS = {"transfer": "[Transfer]", "red_packet": "[Red packet]"}
_DISPLAY_PATHS = (
    "title",
    "des",
    "wcpayinfo/feedesc",
    "wcpayinfo/pay_memo",
    "wcpayinfo/receivertitle",
    "wcpayinfo/sendertitle",
    "wcpayinfo/senderdes",
    "wcpayinfo/receiverdes",
    "wcpayinfo/redenvelopedesc",
)
_SCHEMA_FIELDS = {"schema_version", "kind", "appmsg_subtype", "display_fields"}
MAX_PAYMENT_TEXT_BYTES = (
    MAX_PAYMENT_FIELDS_BYTES
    + len(_DISPLAY_PATHS)
    + max(len(label.encode("utf-8")) for label in _PAYMENT_LABELS.values())
)
_CURRENCY_PATHS = (
    "wcpayinfo/feedesc",
    "wcpayinfo/receivertitle",
    "wcpayinfo/sendertitle",
    "wcpayinfo/senderdes",
    "wcpayinfo/receiverdes",
)
_PRESENTATION_LABELS = {"transfer": "Transfer", "red_packet": "Red packet"}
_REGIONAL_TRANSFER_COPY = (
    ("title", "给你转账"),
    ("des", "WeChat转账"),
    ("wcpayinfo/receivertitle", "Transfer to you"),
)
_NATIVE_TEXT_KINDS = {"app_text", "text_appmsg"}
_CURRENCY_MARKERS = (
    (
        "CNY",
        re.compile(r"(?<![A-Za-z])(?:CNY|RMB)(?![A-Za-z])|CN[¥￥]|人民币|人民幣", re.I),
    ),
    (
        "HKD",
        re.compile(r"(?<![A-Za-z])HKD(?![A-Za-z])|HK\$|港[币幣元圆圓]", re.I),
    ),
)
_OTHER_CURRENCY_TERMS = (
    r"美元|美金|日[元圆圓]|欧元|歐元|英[镑鎊]|澳[元币幣]|加[元币幣]|"
    r"新?台[币幣]|澳门[元币]|澳門[元幣]|[韩韓]元|越南盾|[卢盧]布|泰[铢銖]"
)
_OTHER_ISO_CODES = (
    r"USD|EUR|JPY|GBP|AUD|CAD|SGD|CHF|TWD|VND|KRW|MOP|NZD|THB|MYR|INR|IDR|"
    r"RUB|ZAR|AED|SAR|PHP|BND|KHR|LAK|MMK|PKR|BDT"
)
_OTHER_CURRENCY = re.compile(
    r"(?<![A-Za-z])(?:[A-Z]{3}|(?i:" + _OTHER_ISO_CODES + r"))"
    r"(?![A-Za-z])|[A-Za-z]{1,3}[¥￥$]|" + _OTHER_CURRENCY_TERMS + r"|[¥￥$€£₩₹]"
)
_AMOUNT_MARKER = (
    r"(?:[A-Za-z]{1,3}[¥￥$]|(?<![A-Za-z])(?:[A-Z]{3}|(?i:CNY|RMB|HKD|"
    + _OTHER_ISO_CODES
    + r"))(?![A-Za-z])|人民币|人民幣|港[币幣元圆圓]|"
    + _OTHER_CURRENCY_TERMS
    + r"|[¥￥$€£₩₹元])"
)
_AMOUNT_LINE = re.compile(
    r"(?P<prefix>(?:" + _AMOUNT_MARKER + r"\s*)*+)"
    r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?"
    r"(?P<suffix>(?:\s*" + _AMOUNT_MARKER + r")*+)"
)


def _text_bytes(value: Any, limit: int) -> int:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("Payment-card source fields must be literal strings.")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError(
            "Payment-card source fields must contain valid Unicode."
        ) from error
    if length > limit:
        raise ValueError("Payment-card source text exceeds its supported size.")
    return length


def validate_payment_card_fields(value: Any) -> dict[str, Any]:
    """Return fresh, bounded source metadata; reject unknown fields and type claims."""
    if not isinstance(value, dict) or set(value) != _SCHEMA_FIELDS:
        raise ValueError("Payment-card metadata has an unsupported schema.")
    subtype = value["appmsg_subtype"]
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != PAYMENT_SCHEMA_VERSION
        or type(subtype) is not int
        or subtype not in _PAYMENT_KINDS
        or value["kind"] != _PAYMENT_KINDS[subtype]
    ):
        raise ValueError("Payment-card metadata has an unsupported native type.")
    fields = value["display_fields"]
    if not isinstance(fields, dict) or set(fields) - set(_DISPLAY_PATHS):
        raise ValueError("Payment-card metadata contains unsupported source fields.")
    total = 0
    copied = {}
    for path in _DISPLAY_PATHS:
        if path not in fields:
            continue
        literal = fields[path]
        total += _text_bytes(literal, MAX_PAYMENT_FIELD_BYTES)
        if total > MAX_PAYMENT_FIELDS_BYTES:
            raise ValueError("Payment-card metadata exceeds its supported size.")
        copied[path] = literal
    return {
        "schema_version": PAYMENT_SCHEMA_VERSION,
        "kind": _PAYMENT_KINDS[subtype],
        "appmsg_subtype": subtype,
        "display_fields": copied,
    }


def payment_card_text(value: Any) -> str:
    """Return the exact generated card copy from fresh, validated schema 1 metadata."""
    metadata = validate_payment_card_fields(value)
    lines = [_PAYMENT_LABELS[metadata["kind"]]]
    for literal in metadata["display_fields"].values():
        if literal.strip() and literal not in lines:
            lines.append(literal)
    return "\n".join(lines)


def _bounded_tree(root: ElementTree.Element) -> bool:
    """Keep this pure tree adapter bounded even when called outside the XML parser."""
    pending, seen, total = [(root, 1)], set(), 0
    while pending:
        node, depth = pending.pop()
        if id(node) in seen or depth > MAX_PAYMENT_XML_DEPTH:
            return False
        seen.add(id(node))
        if len(seen) > MAX_PAYMENT_XML_NODES:
            return False
        try:
            total += _text_bytes(
                node.text if node.text is not None else "", MAX_PAYMENT_XML_BYTES
            )
            total += _text_bytes(
                node.tail if node.tail is not None else "", MAX_PAYMENT_XML_BYTES
            )
        except ValueError:
            return False
        if total > MAX_PAYMENT_XML_BYTES:
            return False
        pending.extend((child, depth + 1) for child in node)
    return True


def _unique_leaf(root: ElementTree.Element, path: str) -> str | None:
    node = root
    for part in path.split("/"):
        matches = node.findall(part)
        if len(matches) > 1:
            raise ValueError("Payment-card metadata contains duplicate source fields.")
        if not matches:
            return None
        node = matches[0]
    if len(node):
        raise ValueError("Payment-card display fields must not contain nested markup.")
    return node.text if node.text is not None else ""


def extract_payment_card(
    appmsg: ElementTree.Element, subtype: int
) -> dict[str, Any] | None:
    """Read an already parsed native payment envelope without opening payment links.

    Literal fields describe the retained card, not a verified balance, settled
    transaction, claim state, or relationship between payment participants.
    """
    if (
        not isinstance(appmsg, ElementTree.Element)
        or appmsg.tag != "appmsg"
        or type(subtype) is not int
        or subtype not in _PAYMENT_KINDS
        or not _bounded_tree(appmsg)
    ):
        return None
    try:
        source_type = _unique_leaf(appmsg, "type")
        if (
            source_type is None
            or _TYPE.fullmatch(source_type.strip()) is None
            or int(source_type.strip()) != subtype
        ):
            return None
        envelopes = appmsg.findall("wcpayinfo")
        if len(envelopes) != 1:
            return None
        fields = {}
        for path in _DISPLAY_PATHS:
            literal = _unique_leaf(appmsg, path)
            if literal is not None:
                fields[path] = literal
        metadata = validate_payment_card_fields(
            {
                "schema_version": PAYMENT_SCHEMA_VERSION,
                "kind": _PAYMENT_KINDS[subtype],
                "appmsg_subtype": subtype,
                "display_fields": fields,
            }
        )
    except (ValueError, TypeError):
        return None
    flags = ["payment_card_metadata", "financial_status_unverified"]
    if not metadata["display_fields"].get("wcpayinfo/feedesc", "").strip():
        flags.append("amount_unavailable")
    return {
        "text": payment_card_text(metadata),
        "payment_card_fields": metadata,
        "flags": flags,
    }


def _currency_projection(fields):
    """Use explicit retained markers, leaving a bare yen or dollar sign ambiguous."""
    currencies, evidence, seen, unresolved = set(), [], set(), set()
    for path, literal in fields:
        masked = list(literal)
        for currency, pattern in _CURRENCY_MARKERS:
            for match in pattern.finditer(literal):
                marker = match.group()
                if marker.upper() in {"HK$", "CN¥", "CN￥"}:
                    before, after = (
                        literal[: match.start()].rstrip(),
                        literal[match.end() :].lstrip(),
                    )
                    if not (
                        (before and before[-1] in "0123456789")
                        or (after and re.match(r"[+-]?[0-9]", after))
                    ):
                        continue
                currencies.add(currency)
                key = (path, marker.casefold())
                if key not in seen and len(evidence) < 16:
                    evidence.append({"path": path, "marker": marker})
                    seen.add(key)
                masked[match.start() : match.end()] = " " * (
                    match.end() - match.start()
                )
        for match in _OTHER_CURRENCY.finditer("".join(masked)):
            marker = match.group()
            unresolved.add(marker)
            key = (path, marker)
            if key not in seen and len(evidence) < 16:
                evidence.append({"path": path, "marker": marker})
                seen.add(key)
    currency = next(iter(currencies)) if len(currencies) == 1 else ""
    if unresolved - {"CNY": {"¥", "￥"}, "HKD": {"$"}}.get(currency, set()):
        currency = ""
    return currency, evidence


def _regional_transfer_evidence(metadata):
    """Recognize the complete observed regional provider tuple, not freeform copy.

    One retained HKD transfer uses subtype 2001. Its provider title, description,
    and both party-facing titles identify a transfer despite the legacy schema's
    subtype-derived red-packet kind. Preserve that source kind and generated text.
    """
    if metadata["appmsg_subtype"] != 2001:
        return []
    fields = metadata["display_fields"]
    if any(fields.get(path) != literal for path, literal in _REGIONAL_TRANSFER_COPY):
        return []
    sender = fields.get("wcpayinfo/sendertitle", "")
    prefix = "Transfer to "
    if (
        not sender.startswith(prefix)
        or not sender[len(prefix) :].strip()
        or len(sender.splitlines()) != 1
    ):
        return []
    provider_copy = "\n".join(
        fields[path] for path in ("title", "des", *_CURRENCY_PATHS) if path in fields
    )
    if re.search(r"red[\s-]*packet|红包|紅包", provider_copy, re.I):
        return []
    return [path for path, _ in _REGIONAL_TRANSFER_COPY] + ["wcpayinfo/sendertitle"]


def payment_presentation(provenance: Any, *, text: str = "") -> dict[str, Any] | None:
    """Project a payment icon from validated source roles without altering source copy.

    Dedicated native amount and payment-status fields can provide an explicit
    currency marker; generic descriptions, greetings, and memos cannot.
    Legacy native app text contributes only complete amount lines; remarks,
    greetings, undocumented numeric fields, and payment outcomes are not inferred.
    A bare yen sign does not distinguish CNY from JPY, and a bare dollar sign does
    not distinguish HKD from other dollar currencies.
    When nonempty source text is supplied with typed metadata, it must match the
    exact generated card copy. The empty default supports metadata-only callers.
    A complete observed regional transfer tuple can refine subtype 2001's display
    kind; the original schema kind and exact supporting field paths remain visible.
    """
    if not isinstance(provenance, dict):
        return None
    for key in ("message_type", "original_message_type"):
        if key in provenance and (
            type(provenance[key]) is not int or provenance[key] != 49
        ):
            return None
    try:
        if "payment_card_fields" in provenance:
            metadata = validate_payment_card_fields(provenance["payment_card_fields"])
            _text_bytes(text, MAX_PAYMENT_TEXT_BYTES)
            if text and text != payment_card_text(metadata):
                return None
            if "appmsg_subtype" in provenance and (
                type(provenance["appmsg_subtype"]) is not int
                or provenance["appmsg_subtype"] != metadata["appmsg_subtype"]
            ):
                return None
            if "text_extraction_kind" in provenance and (
                provenance["text_extraction_kind"] not in _NATIVE_TEXT_KINDS
            ):
                return None
            kind, source = metadata["kind"], "payment_card_fields"
            fields = [
                (path, metadata["display_fields"][path])
                for path in _CURRENCY_PATHS
                if path in metadata["display_fields"]
            ]
        else:
            subtype = provenance.get("appmsg_subtype")
            if (
                type(subtype) is not int
                or subtype not in _PAYMENT_KINDS
                or provenance.get("text_extraction_kind") not in _NATIVE_TEXT_KINDS
                or not any(
                    provenance.get(key) == 49
                    for key in ("message_type", "original_message_type")
                )
            ):
                return None
            _text_bytes(text, MAX_PAYMENT_FIELDS_BYTES)
            kind, source = _PAYMENT_KINDS[subtype], "native_app_text"
            fields = []
            for number, line in enumerate(text.splitlines(), 1):
                literal = line.strip()
                if len(literal.encode("utf-8")) > MAX_PAYMENT_FIELD_BYTES:
                    continue
                match = _AMOUNT_LINE.fullmatch(literal)
                if match and (match["prefix"] or match["suffix"]):
                    fields.append(("text_line:" + str(number), literal))
        currency, evidence = _currency_projection(fields)
        kind_evidence = (
            _regional_transfer_evidence(metadata)
            if source == "payment_card_fields"
            else []
        )
        source_kind = kind
        if kind_evidence:
            kind = "transfer"
    except (TypeError, ValueError):
        return None
    presentation = {
        "kind": kind,
        "label": _PRESENTATION_LABELS[kind],
        "currency": currency,
        "source": source,
        "currency_evidence": evidence,
    }
    if kind_evidence:
        presentation.update(source_kind=source_kind, kind_evidence=kind_evidence)
    return presentation
