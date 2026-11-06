"""Native payment-card copy remains literal, bounded, and transaction-neutral.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
from html import escape
from xml.etree import ElementTree

import pytest

from app.core import wechat_payments as payments


def _card(body="", *, subtype=2000, payment_body=""):
    return ElementTree.fromstring(
        f"<appmsg><type>{subtype}</type>{body}"
        f"<wcpayinfo>{payment_body}</wcpayinfo></appmsg>"
    )


def _metadata(**updates):
    return {
        "schema_version": 1,
        "kind": "transfer",
        "appmsg_subtype": 2000,
        "display_fields": {"title": "Original payment-card title"},
        **updates,
    }


def test_transfer_copy_preserves_source_whitespace_currency_and_status_words():
    title = "  Transfer card\nLiteral title  "
    memo = " dinner\n\nshared cost "
    amount = "￥0005.20"
    source_status = "Original source status words"
    appmsg = _card(
        "<title>" + escape(title) + "</title><des>Source description</des>",
        payment_body="<feedesc>"
        + escape(amount)
        + "</feedesc><pay_memo>"
        + escape(memo)
        + "</pay_memo><receivertitle>"
        + escape(source_status)
        + "</receivertitle>",
    )
    before = ElementTree.tostring(appmsg)
    result = payments.extract_payment_card(appmsg, 2000)
    assert result is not None
    assert result["text"] == "\n".join(
        ["[Transfer]", title, "Source description", amount, memo, source_status]
    )
    assert result["payment_card_fields"] == _metadata(
        display_fields={
            "title": title,
            "des": "Source description",
            "wcpayinfo/feedesc": amount,
            "wcpayinfo/pay_memo": memo,
            "wcpayinfo/receivertitle": source_status,
        }
    )
    assert result["flags"] == ["payment_card_metadata", "financial_status_unverified"]
    assert ElementTree.tostring(appmsg) == before


def test_duplicate_display_copy_is_shown_once_but_each_literal_field_survives():
    appmsg = _card(
        "<title>Identical card copy</title><des>Identical card copy</des>",
        payment_body="<sendertitle>Identical card copy</sendertitle>",
    )
    result = payments.extract_payment_card(appmsg, 2000)
    assert result["text"] == "[Transfer]\nIdentical card copy"
    assert result["payment_card_fields"]["display_fields"] == {
        "title": "Identical card copy",
        "des": "Identical card copy",
        "wcpayinfo/sendertitle": "Identical card copy",
    }


def test_token_urls_and_non_display_payment_identifiers_are_never_copied():
    appmsg = _card(
        "<title>Retained title</title><url>https://example.invalid/?token=secret</url>",
        payment_body="<nativeurl>wxpay://claim?token=secret</nativeurl>"
        "<transferid>private-transfer-id</transferid><payer_username>wxid_payer</payer_username>"
        "<fee>123456</fee><paysubtype>3</paysubtype>",
    )
    result = payments.extract_payment_card(appmsg, 2000)
    assert result["text"] == "[Transfer]\nRetained title"
    assert result["payment_card_fields"]["display_fields"] == {
        "title": "Retained title"
    }
    assert "secret" not in str(result)
    assert "private-transfer-id" not in str(result)
    assert "wxid_payer" not in str(result)
    assert "123456" not in str(result)
    assert "settled" not in result and "received" not in result


def test_xml_looking_display_copy_stays_literal_and_is_not_parsed_again():
    literal = '<script>not executed</script> & "source words"'
    appmsg = _card("<title><![CDATA[" + literal + "]]></title>")
    result = payments.extract_payment_card(appmsg, 2000)
    assert result["text"] == "[Transfer]\n" + literal
    assert result["payment_card_fields"]["display_fields"]["title"] == literal


def test_missing_amount_never_uses_fee_or_a_parent_clock_as_amount():
    result = payments.extract_payment_card(
        _card(
            payment_body="<fee>520</fee><begintransfertime>1700000000</begintransfertime>"
        ),
        2000,
    )
    assert result["text"] == "[Transfer]"
    assert result["payment_card_fields"]["display_fields"] == {}
    assert result["flags"] == [
        "payment_card_metadata",
        "financial_status_unverified",
        "amount_unavailable",
    ]


def test_observed_red_packet_shape_preserves_copy_without_interpreting_numeric_amounts():
    appmsg = _card(
        "<title>Original red-packet title</title><des>Original greeting</des>",
        subtype=2001,
        payment_body="<receivertitle>Received-card source wording</receivertitle>"
        "<sendertitle>Sender-card source wording</sendertitle>"
        "<senderdes>Original sender description</senderdes>"
        "<receiverdes>Original receiver description</receiverdes>"
        "<scenetext>First internal field</scenetext><scenetext>Second internal field</scenetext>"
        "<nativeurl>wxpay://claim?secret=token</nativeurl><feedesc/>"
        "<invalidtime>1700000000</invalidtime><effectivedate>1700000001</effectivedate>"
        "<redenvelopereceiveamount>200</redenvelopereceiveamount><receivestatus>2</receivestatus>",
    )
    before = ElementTree.tostring(appmsg)
    result = payments.extract_payment_card(appmsg, 2001)
    assert result["text"] == "\n".join(
        [
            "[Red packet]",
            "Original red-packet title",
            "Original greeting",
            "Received-card source wording",
            "Sender-card source wording",
            "Original sender description",
            "Original receiver description",
        ]
    )
    assert result["payment_card_fields"] == {
        "schema_version": 1,
        "kind": "red_packet",
        "appmsg_subtype": 2001,
        "display_fields": {
            "title": "Original red-packet title",
            "des": "Original greeting",
            "wcpayinfo/feedesc": "",
            "wcpayinfo/receivertitle": "Received-card source wording",
            "wcpayinfo/sendertitle": "Sender-card source wording",
            "wcpayinfo/senderdes": "Original sender description",
            "wcpayinfo/receiverdes": "Original receiver description",
        },
    }
    assert "amount_unavailable" in result["flags"]
    assert "financial_status_unverified" in result["flags"]
    assert "token" not in str(result)
    assert "1700000000" not in str(result)
    assert "redenvelopereceiveamount" not in str(result)
    assert "scenetext" not in str(result)
    assert ElementTree.tostring(appmsg) == before


@pytest.mark.parametrize("subtype", [1, 5, 19, 57, 2002, 99999, True, "2000"])
def test_unknown_or_unverified_subtypes_are_not_enabled_by_visible_payment_copy(
    subtype,
):
    assert (
        payments.extract_payment_card(_card("<title>Transfer</title>"), subtype) is None
    )


@pytest.mark.parametrize(
    "xml",
    [
        "<appmsg><type>2000</type><title>Payment-looking words</title></appmsg>",
        "<appmsg><type>2000</type><wcpayinfo/><wcpayinfo/></appmsg>",
        "<appmsg><type>2000</type><type>2000</type><wcpayinfo/></appmsg>",
        "<appmsg><type><nested>2000</nested></type><wcpayinfo/></appmsg>",
        "<appmsg><type>2001</type><wcpayinfo/></appmsg>",
        "<msg><appmsg><type>2000</type><wcpayinfo/></appmsg></msg>",
    ],
)
def test_envelope_type_and_unique_payment_container_are_required(xml):
    assert payments.extract_payment_card(ElementTree.fromstring(xml), 2000) is None


@pytest.mark.parametrize(
    "path",
    [
        "title",
        "des",
        "wcpayinfo/feedesc",
        "wcpayinfo/pay_memo",
        "wcpayinfo/receivertitle",
    ],
)
@pytest.mark.parametrize("damage", ["duplicate", "nested"])
def test_ambiguous_or_nested_display_fields_reject_the_complete_card(path, damage):
    appmsg = _card()
    parent = appmsg
    parts = path.split("/")
    if len(parts) == 2:
        parent = appmsg.find(parts[0])
    leaf = ElementTree.SubElement(parent, parts[-1])
    leaf.text = "Source words"
    if damage == "duplicate":
        ElementTree.SubElement(parent, parts[-1]).text = "Source words"
    else:
        ElementTree.SubElement(leaf, "nested").text = "configuration"
    assert payments.extract_payment_card(appmsg, 2000) is None


def test_metadata_validation_returns_a_fresh_copy_without_changing_source_values():
    original = _metadata(display_fields={"title": "  Literal source  ", "des": ""})
    before = deepcopy(original)
    validated = payments.validate_payment_card_fields(original)
    assert validated == original and original == before
    validated["display_fields"]["title"] = "Changed returned copy"
    assert original == before


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"appmsg_subtype": True},
        {"appmsg_subtype": "2000"},
        {"appmsg_subtype": 2001},
        {"kind": "red_packet"},
        {"kind": None},
        {"display_fields": []},
        {"display_fields": {"wcpayinfo/nativeurl": "wxpay://secret"}},
        {"display_fields": {"title": 0}},
        {"display_fields": {"title": False}},
        {"display_fields": {"title": None}},
        {"display_fields": {"title": "bad\0text"}},
        {"display_fields": {"title": "bad\ud800text"}},
        {"display_fields": {"title": "x" * (payments.MAX_PAYMENT_FIELD_BYTES + 1)}},
    ],
)
def test_metadata_validation_rejects_unknown_types_paths_and_nonliteral_values(updates):
    with pytest.raises(ValueError):
        payments.validate_payment_card_fields(_metadata(**updates))


@pytest.mark.parametrize("damage", ["extra", "missing", "not_object"])
def test_metadata_schema_is_exact(damage):
    value = _metadata()
    if damage == "extra":
        value["claimed"] = True
    elif damage == "missing":
        value.pop("kind")
    else:
        value = []
    with pytest.raises(ValueError):
        payments.validate_payment_card_fields(value)


def test_field_and_total_byte_limits_count_utf8_bytes():
    with pytest.raises(ValueError):
        payments.validate_payment_card_fields(
            _metadata(
                display_fields={
                    "title": "字" * (payments.MAX_PAYMENT_FIELD_BYTES // 3 + 1)
                }
            )
        )
    value = "x" * payments.MAX_PAYMENT_FIELD_BYTES
    with pytest.raises(ValueError):
        payments.validate_payment_card_fields(
            _metadata(display_fields={path: value for path in payments._DISPLAY_PATHS})
        )


@pytest.mark.parametrize("kind", ["oversize", "depth", "nodes", "cycle", "nonstring"])
def test_tree_work_stays_bounded_even_for_manually_constructed_elements(kind):
    appmsg = _card()
    if kind == "oversize":
        ElementTree.SubElement(appmsg, "ignored").text = "x" * (
            payments.MAX_PAYMENT_XML_BYTES + 1
        )
    elif kind == "depth":
        node = appmsg
        for _ in range(payments.MAX_PAYMENT_XML_DEPTH):
            node = ElementTree.SubElement(node, "ignored")
    elif kind == "nodes":
        for _ in range(payments.MAX_PAYMENT_XML_NODES):
            ElementTree.SubElement(appmsg, "ignored")
    elif kind == "cycle":
        appmsg.append(appmsg)
    else:
        appmsg.find("type").text = 0
    assert payments.extract_payment_card(appmsg, 2000) is None


@pytest.mark.parametrize("subtype,kind", [(2000, "transfer"), (2001, "red_packet")])
def test_known_native_payment_text_and_metadata_round_trip_through_existing_pipeline(
    subtype, kind
):
    from app.core.wechat_reconcile import native_text_metadata
    from app.core.wechat_text import extract_wechat_text

    element = _card(
        "<title>Retained native card</title>",
        subtype=subtype,
        payment_body="<feedesc>¥0005.20</feedesc><pay_memo>Literal memo</pay_memo>"
        "<nativeurl>wxpay://ignored?secret=token</nativeurl>",
    )
    raw = ElementTree.tostring(element, encoding="unicode")
    result = extract_wechat_text(49, raw)
    assert result["text_extraction_kind"] == "app_text"
    assert result["original_message_type"] == 49
    assert result["appmsg_subtype"] == subtype
    assert result["payment_card_fields"]["kind"] == kind
    assert (
        result["payment_card_fields"]["display_fields"]["wcpayinfo/feedesc"]
        == "¥0005.20"
    )
    metadata = native_text_metadata(result, {})
    assert metadata["payment_card_fields"] == result["payment_card_fields"]
    assert metadata["appmsg_subtype"] == subtype
    assert "token" not in str(result)
    assert "financial_status_unverified" in result["flags"]
    assert "amount_unavailable" not in result["flags"]


def test_native_metadata_validation_rejects_a_token_url_path_without_mutating_input():
    from app.core.wechat_reconcile import native_text_metadata

    record = {
        "appmsg_subtype": 2000,
        "payment_card_fields": _metadata(
            display_fields={
                "title": "Literal title",
                "wcpayinfo/nativeurl": "wxpay://claim?secret=token",
            }
        ),
    }
    before = deepcopy(record)
    with pytest.raises(ValueError):
        native_text_metadata(record, {})
    assert record == before


def _native_proof(*, subtype=2000):
    return {
        "message_type": 49,
        "appmsg_subtype": subtype,
        "text_extraction_kind": "app_text",
    }


@pytest.mark.parametrize(
    "amount,currency,marker",
    [
        ("CNY 8.00", "CNY", "CNY"),
        ("8.00 RMB", "CNY", "RMB"),
        ("人民币 8.00", "CNY", "人民币"),
        ("8.00 人民幣", "CNY", "人民幣"),
        ("CN¥8.00", "CNY", "CN¥"),
        ("CNY ¥8.00", "CNY", "CNY"),
        ("HKD 8.00", "HKD", "HKD"),
        ("HK$8.00", "HKD", "HK$"),
        ("港币 8.00", "HKD", "港币"),
        ("8.00 港幣", "HKD", "港幣"),
        ("8.00 港元", "HKD", "港元"),
        ("HKD $8.00", "HKD", "HKD"),
    ],
)
def test_payment_projection_uses_explicit_amount_field_currency(
    amount, currency, marker
):
    proof = {
        **_native_proof(),
        "payment_card_fields": _metadata(
            display_fields={
                "wcpayinfo/feedesc": amount,
                "wcpayinfo/pay_memo": "USD 999",
            }
        ),
    }
    before = deepcopy(proof)
    text = payments.payment_card_text(proof["payment_card_fields"])
    result = payments.payment_presentation(proof, text=text)
    assert result["kind"] == "transfer"
    assert result["label"] == "Transfer"
    assert result["currency"] == currency
    assert result["source"] == "payment_card_fields"
    assert {"path": "wcpayinfo/feedesc", "marker": marker} in result[
        "currency_evidence"
    ]
    assert set(result) == {"kind", "label", "currency", "source", "currency_evidence"}
    assert proof == before


def test_typed_presentation_requires_exact_supplied_card_copy():
    metadata = _metadata(
        display_fields={"title": "  Literal source  ", "wcpayinfo/feedesc": "HK$8.00"}
    )
    proof = {**_native_proof(), "payment_card_fields": metadata}
    before = deepcopy(proof)
    literal = "[Transfer]\n  Literal source  \nHK$8.00"
    assert payments.payment_card_text(metadata) == literal
    assert payments.payment_presentation(proof, text=literal)["currency"] == "HKD"
    assert payments.payment_presentation(proof)["currency"] == "HKD"
    for text in (
        literal.replace("  Literal source  ", "Literal source"),
        "[Transfer]\nJPY 8",
        literal + "\n",
        None,
    ):
        assert payments.payment_presentation(proof, text=text) is None
    assert proof == before


@pytest.mark.parametrize(
    "amount",
    [
        "¥8.00",
        "￥8.00",
        "$8.00",
        "8.00 元",
        "JPY 8",
        "usd 8",
        "",
        "CNY 8 HKD 9",
        "HKD 8 USD 9",
        "HKD 8 jpy 9",
        "HKD ¥8",
        "CNY $8",
        "HKD 8 NZ$9",
        "CNY 8 JP¥9",
        "CNY 8 日元 9",
    ],
)
def test_unknown_or_conflicting_currency_never_defaults_to_the_account_currency(amount):
    proof = {
        **_native_proof(subtype=2001),
        "account_id": "wxid_hong_kong_label",
        "payment_card_fields": _metadata(
            kind="red_packet",
            appmsg_subtype=2001,
            display_fields={
                "title": "HKD red packet",
                "wcpayinfo/feedesc": amount,
                "wcpayinfo/pay_memo": "CNY 99",
                "des": "人民币 account label",
            },
        ),
    }
    result = payments.payment_presentation(proof)
    assert result["currency"] == ""
    assert (result["kind"], result["label"]) == ("red_packet", "Red packet")


@pytest.mark.parametrize(
    "fields,currency",
    [
        (
            {
                "wcpayinfo/receiverdes": "You've received HK$0.01 from a red packet",
                "wcpayinfo/senderdes": "A recipient received HK$0.01",
            },
            "HKD",
        ),
        ({"wcpayinfo/receivertitle": "人民币红包"}, "CNY"),
        ({"wcpayinfo/sendertitle": "人民幣紅包"}, "CNY"),
        ({"wcpayinfo/receiverdes": "HKD red packet"}, "HKD"),
        ({"wcpayinfo/receiverdes": "Payment in HK$"}, ""),
        (
            {
                "wcpayinfo/receiverdes": "Received HK$0.01",
                "wcpayinfo/sendertitle": "人民币红包",
            },
            "",
        ),
        ({"wcpayinfo/redenvelopedesc": "HKD 999 greeting"}, ""),
    ],
)
def test_dedicated_red_packet_status_fields_provide_currency_without_claiming_status(
    fields, currency
):
    proof = {
        **_native_proof(subtype=2001),
        "payment_card_fields": _metadata(
            kind="red_packet",
            appmsg_subtype=2001,
            display_fields=fields,
        ),
    }
    before = deepcopy(proof)
    result = payments.payment_presentation(proof)
    assert result["currency"] == currency
    assert result["kind"] == "red_packet"
    assert set(result) == {"kind", "label", "currency", "source", "currency_evidence"}
    assert proof == before


@pytest.mark.parametrize(
    "text,currency",
    [
        ("WeChat transfer\nHK$1,234.50\nLiteral remark", "HKD"),
        ("WeChat transfer\n0008.00 RMB", "CNY"),
        ("Red packet\n8.00 港元", "HKD"),
        ("WeChat transfer\n￥8.00", ""),
        ("WeChat transfer\nHKD 8\nCNY 9", ""),
        ("WeChat transfer\nCNY 8\nJPY 9", ""),
        ("WeChat transfer\nCNY 8\n9 日元", ""),
        ("WeChat transfer\nHKD 8 for dinner", ""),
        ("WeChat transfer\nMemo: send HKD 8 later", ""),
    ],
)
def test_legacy_native_text_uses_only_complete_literal_amount_lines(text, currency):
    proof = _native_proof()
    before = deepcopy(proof)
    result = payments.payment_presentation(proof, text=text)
    assert result["currency"] == currency
    assert result["source"] == "native_app_text"
    assert result["label"] == "Transfer"
    assert proof == before
    assert all(
        item["path"].startswith("text_line:") for item in result["currency_evidence"]
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"message_type": 1},
        {"message_type": True},
        {"message_type": "49"},
        {"original_message_type": 1},
        {"appmsg_subtype": 1},
        {"appmsg_subtype": True},
        {"appmsg_subtype": "2000"},
        {"text_extraction_kind": "plain_text"},
        {"text_extraction_kind": "rendered_app_text"},
        {"text_extraction_kind": []},
    ],
)
def test_visible_payment_words_do_not_override_unsupported_source_roles(updates):
    proof = {**_native_proof(), **updates}
    assert payments.payment_presentation(proof, text="[Transfer]\nHKD 8") is None


def test_malformed_card_metadata_does_not_fall_back_to_payment_looking_text():
    proof = {
        **_native_proof(),
        "payment_card_fields": _metadata(display_fields={"fee": "800"}),
    }
    assert payments.payment_presentation(proof, text="[Transfer]\nHKD 8") is None
    proof["payment_card_fields"] = _metadata(appmsg_subtype=2001, kind="red_packet")
    assert payments.payment_presentation(proof, text="[Transfer]\nHKD 8") is None
    assert payments.payment_presentation({"appmsg_subtype": 2000}, text="HKD 8") is None
    assert (
        payments.payment_presentation({"message_type": 49}, text="[Transfer]\nHKD 8")
        is None
    )


def test_projection_ignores_undocumented_amount_and_settlement_fields():
    proof = {
        **_native_proof(),
        "fee": 800,
        "receivestatus": 2,
        "paysubtype": 3,
        "begintransfertime": 1700000000,
        "payment_card_fields": _metadata(),
    }
    before = deepcopy(proof)
    assert payments.payment_presentation(proof) == {
        "kind": "transfer",
        "label": "Transfer",
        "currency": "",
        "source": "payment_card_fields",
        "currency_evidence": [],
    }
    assert proof == before


@pytest.mark.parametrize(
    "text", [None, "bad\0text", "bad\ud800text", "x" * (64 * 1024 + 1)]
)
def test_legacy_presentation_input_is_bounded_literal_unicode(text):
    assert payments.payment_presentation(_native_proof(), text=text) is None


def test_currency_evidence_is_bounded_even_for_repeated_amount_lines():
    result = payments.payment_presentation(_native_proof(), text="HKD 8\n" * 1000)
    assert result["currency"] == "HKD"
    assert len(result["currency_evidence"]) == 16
    conflicting = payments.payment_presentation(
        _native_proof(), text="HKD 8\n" * 1000 + "JPY 9"
    )
    assert conflicting["currency"] == ""
    assert len(conflicting["currency_evidence"]) == 16


def test_valid_metadata_at_byte_budget_can_verify_its_generated_copy():
    fields = {
        path: str(number) + "x" * (payments.MAX_PAYMENT_FIELD_BYTES - 1)
        for number, path in enumerate(payments._DISPLAY_PATHS[:8])
    }
    metadata = _metadata(display_fields=fields)
    text = payments.payment_card_text(metadata)
    assert len(text.encode("utf-8")) > payments.MAX_PAYMENT_FIELDS_BYTES
    proof = {**_native_proof(), "payment_card_fields": metadata}
    assert payments.payment_presentation(proof, text=text) is not None


def test_repeated_marker_noise_does_not_expand_legacy_amount_matching():
    result = payments.payment_presentation(_native_proof(), text="HKD$" * 2000)
    assert result["currency"] == ""
    assert result["currency_evidence"] == []


def _regional_transfer_metadata():
    return _metadata(
        kind="red_packet",
        appmsg_subtype=2001,
        display_fields={
            "title": "给你转账",
            "des": "WeChat转账",
            "wcpayinfo/receivertitle": "Transfer to you",
            "wcpayinfo/sendertitle": "Transfer to Fixture recipient",
            "wcpayinfo/receiverdes": "HK$0.19",
            "wcpayinfo/senderdes": "HK$0.19",
        },
    )


def test_observed_hkd_regional_transfer_refines_only_presentation_kind():
    metadata = _regional_transfer_metadata()
    proof = {**_native_proof(subtype=2001), "payment_card_fields": metadata}
    before = deepcopy(proof)
    text = payments.payment_card_text(metadata)
    result = payments.payment_presentation(proof, text=text)
    assert result == {
        "kind": "transfer",
        "label": "Transfer",
        "currency": "HKD",
        "source": "payment_card_fields",
        "source_kind": "red_packet",
        "kind_evidence": [
            "title",
            "des",
            "wcpayinfo/receivertitle",
            "wcpayinfo/sendertitle",
        ],
        "currency_evidence": [
            {"path": "wcpayinfo/senderdes", "marker": "HK$"},
            {"path": "wcpayinfo/receiverdes", "marker": "HK$"},
        ],
    }
    assert text.startswith("[Red packet]\n")
    assert payments.payment_card_text(metadata) == text
    assert proof == before
    extracted = payments.extract_payment_card(
        _card(
            "<title>给你转账</title><des>WeChat转账</des>",
            subtype=2001,
            payment_body="<receivertitle>Transfer to you</receivertitle>"
            "<sendertitle>Transfer to Fixture recipient</sendertitle>"
            "<receiverdes>HK$0.19</receiverdes><senderdes>HK$0.19</senderdes>",
        ),
        2001,
    )
    assert extracted["payment_card_fields"] == metadata
    assert extracted["text"] == text


@pytest.mark.parametrize(
    "path,literal",
    [
        ("title", "微信红包"),
        ("title", "给你转账 "),
        ("des", "红包"),
        ("des", "WeChat transfer"),
        ("wcpayinfo/receivertitle", "Red packet to you"),
        ("wcpayinfo/receivertitle", "Transfer to a recipient"),
        ("wcpayinfo/sendertitle", "Transfer to "),
        ("wcpayinfo/sendertitle", "transfer to Fixture recipient"),
        ("wcpayinfo/sendertitle", "Transfer to Recipient\nOther source words"),
        ("wcpayinfo/senderdes", "HK$0.19 red packet"),
        ("wcpayinfo/receiverdes", "收到红包 HK$0.19"),
    ],
)
def test_partial_or_contradictory_regional_provider_copy_keeps_source_kind(
    path, literal
):
    metadata = _regional_transfer_metadata()
    metadata["display_fields"][path] = literal
    proof = {**_native_proof(subtype=2001), "payment_card_fields": metadata}
    before = deepcopy(proof)
    result = payments.payment_presentation(
        proof, text=payments.payment_card_text(metadata)
    )
    assert (result["kind"], result["label"]) == ("red_packet", "Red packet")
    assert "source_kind" not in result and "kind_evidence" not in result
    assert proof == before


def test_memo_or_greeting_cannot_supply_regional_transfer_provider_evidence():
    metadata = _regional_transfer_metadata()
    copy = "\n".join(metadata["display_fields"].values())
    metadata["display_fields"] = {
        "title": "微信红包",
        "wcpayinfo/pay_memo": copy,
        "wcpayinfo/redenvelopedesc": copy,
    }
    proof = {**_native_proof(subtype=2001), "payment_card_fields": metadata}
    result = payments.payment_presentation(proof)
    assert result["kind"] == "red_packet"
    assert result["currency"] == ""
    assert "kind_evidence" not in result


def test_regional_presentation_does_not_change_ambiguous_currency_or_literal_guard():
    metadata = _regional_transfer_metadata()
    metadata["display_fields"].update(
        {"wcpayinfo/senderdes": "¥0.19", "wcpayinfo/receiverdes": "¥0.19"}
    )
    proof = {**_native_proof(subtype=2001), "payment_card_fields": metadata}
    text = payments.payment_card_text(metadata)
    result = payments.payment_presentation(proof, text=text)
    assert result["kind"] == "transfer"
    assert result["currency"] == ""
    assert result["source_kind"] == "red_packet"
    assert (
        payments.payment_presentation(
            proof, text=text.replace("[Red packet]", "[Transfer]")
        )
        is None
    )
