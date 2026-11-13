"""Typed payment cards localize provider copy while retaining literal source records.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import csv
from datetime import datetime
from io import StringIO
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from xml.sax.saxutils import escape

import pytest

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.config import CrawlConfig
from app.core.wechat_archive import _identifier, wechat_archive_path
from app.core.wechat_contact_embedded import native_details
from app.core.wechat_reconcile import native_text_metadata
from app.core.wechat_text import extract_wechat_text
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests.test_wechat_reconcile import merge, record, source
from tests.wechat_browser_markup import get_verified_browser_page


LITERAL_MEMO = "  dinner\n\n<script>window.paymentScript=true</script> & snacks  "
REPLY = "  Literal reply\nwith  preserved spacing  "
PEER = "wxid_payment_peer"


def payment_xml(
    subtype,
    amount,
    *,
    memo="Original source copy",
    title="Original payment title",
    description="",
    receiver_title="",
    sender_title="",
    greeting="",
):
    return (
        f"<msg><appmsg><type>{subtype}</type><title>"
        + escape(title)
        + "</title><des>"
        + escape(description)
        + "</des><wcpayinfo><feedesc>"
        + escape(amount)
        + "</feedesc><pay_memo>"
        + escape(memo)
        + "</pay_memo><receivertitle>"
        + escape(receiver_title)
        + "</receivertitle><sendertitle>"
        + escape(sender_title)
        + "</sendertitle><redenvelopedesc>"
        + escape(greeting)
        + "</redenvelopedesc><nativeurl>wxpay://claim?token=secret</nativeurl>"
        "<transferid>private-transfer-id</transferid></wcpayinfo></appmsg></msg>"
    )


def forwarded_payment_xml():
    def item(identifier, card, *, sender=PEER):
        return (
            f'<dataitem datatype="49" dataid="{identifier}">'
            "<sourcename>Original payment participant</sourcename>"
            "<sourcetime>2021-08-07 18:51:03</sourcetime>"
            "<srcMsgCreateTime>1628347863</srcMsgCreateTime><dataitemsource>"
            f"<fromusr>{sender}</fromusr></dataitemsource>" + card + "</dataitem>"
        )

    nested = (
        "<recordinfo><datalist>"
        + item("nested-payment", payment_xml(2000, "HK$ 88.00"))
        + "</datalist></recordinfo>"
    )
    transcript = (
        "<recordinfo><datalist>"
        + item("first-payment", payment_xml(2001, "CNY 12.50"))
        + '<dataitem datatype="17" dataid="nested-packet">'
        + nested
        + "</dataitem></datalist></recordinfo>"
    )
    return (
        "<msg><appmsg><type>19</type><title>Forwarded conversation</title>"
        "<recorditem>" + escape(transcript) + "</recorditem></appmsg></msg>"
    )


def quoted_payment_xml():
    return (
        "<msg><appmsg><type>57</type><title>"
        + escape(REPLY)
        + "</title><refermsg><type>49</type>"
        f"<fromusr>{PEER}</fromusr><displayname>Original payment participant</displayname>"
        "<svrid>18446744073709551615</svrid><createtime>1628347863</createtime>"
        "<content>"
        + escape(payment_xml(2001, "HKD 16.00"))
        + "</content></refermsg></appmsg></msg>"
    )


def payment_fixture(sandbox):
    """Build a disposable archive through the normal extractor and reconciler."""
    root = sandbox / "store"
    primary = source("wxid_payment_owner_a")
    other = source("wxid_payment_owner_b")
    specifications = (
        ("cny-sent", 49, payment_xml(2000, "CNY 1,234.50", memo=LITERAL_MEMO), "sent"),
        ("hkd-received", 49, payment_xml(2001, "HK$ 88.00"), "received"),
        ("unknown-received", 49, payment_xml(2000, "EUR 4.25"), "received"),
        ("conflict-sent", 49, payment_xml(2001, "CNY 8.00 / HKD 9.00"), "sent"),
        (
            "prose",
            1,
            "WeChat transfer CNY 1,234.50 remains ordinary prose.\n"
            "给你转账 / 轉賬 / 轉帳 / 微信红包",
            "received",
        ),
        (
            "other-app-subtype",
            49,
            "<msg><appmsg><type>1</type><title>Transfer HKD 88.00</title></appmsg></msg>",
            "received",
        ),
        ("forwarded", 49, forwarded_payment_xml(), "received"),
        ("quoted", 49, quoted_payment_xml(), "sent"),
    )
    parsed = {}
    observations = []
    for index, (key, message_type, raw, direction) in enumerate(specifications):
        extracted = extract_wechat_text(message_type, raw)
        assert extracted is not None, key
        parsed[key] = extracted
        observations.append(
            record(
                primary,
                key,
                peer=PEER,
                message_type=message_type,
                sender_id=primary["source_account_id"] if direction == "sent" else PEER,
                sender_display_name="Fixture owner"
                if direction == "sent"
                else "Fixture peer",
                direction=direction,
                server_id=str(40000 + index),
                sent_at_utc_iso=f"2021-08-07T11:{index:02d}:03+00:00",
                **extracted,
            )
        )
    observations.append(
        record(
            other,
            "other-owner-payment",
            peer=PEER,
            message_type=49,
            server_id="40000",
            **deepcopy(parsed["cny-sent"]),
        )
    )
    merge(root, [primary, other], observations)
    history = wechat_archive_path(root)
    identifiers = {key: _identifier(primary["source_id"], key) for key in parsed}
    with sqlite3.connect(history) as connection:
        session = connection.execute(
            "SELECT conversation_id FROM messages WHERE id=?",
            (identifiers["cny-sent"],),
        ).fetchone()[0]
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            root,
            computer_use_settings_path=sandbox / "agent.json",
            computer_use_runtime_root=sandbox / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    return SimpleNamespace(
        root=root,
        primary=primary,
        other=other,
        parsed=parsed,
        observations=observations,
        identifiers=identifiers,
        session=session,
        history=history,
        preserved=history.read_bytes(),
        app=app,
    )


def localized_payment_fixture(sandbox):
    """Mix provider language variants with literal user-authored payment copy."""
    original_factory = payment_xml
    memo = LITERAL_MEMO + "\n给你转账\n轉賬\n轉帳"
    greeting = "微信红包\n  原样祝福，不做翻译  "
    variants = {
        (2000, "CNY 1,234.50"): {
            "title": "给你转账",
            "description": "微信转账",
            "memo": memo,
        },
        (2001, "HK$ 88.00"): {
            "title": "微信红包",
            "description": "微信紅包",
            "greeting": greeting,
        },
        (2000, "EUR 4.25"): {
            "title": "不要给你转账",
            "description": "自定义轉賬说明\n  保留空白  ",
        },
        (2001, "CNY 8.00 / HKD 9.00"): {
            "title": "微信紅包",
            "description": "微信红包",
        },
        (2001, "CNY 12.50"): {
            "title": "微信紅包",
            "description": "微信红包",
        },
        (2000, "HK$ 88.00"): {
            "title": "轉賬",
            "description": "微信轉帳",
        },
        (2001, "HKD 16.00"): {
            "title": "微信红包",
            "description": "微信紅包",
        },
    }

    def card(subtype, amount, **kwargs):
        return original_factory(
            subtype,
            amount,
            **{**kwargs, **variants.get((subtype, amount), {})},
        )

    with patch(__name__ + ".payment_xml", side_effect=card):
        fixture = payment_fixture(sandbox)
    fixture.literal_memo = memo
    fixture.literal_greeting = greeting
    return fixture


@pytest.fixture
def payment_store(tmp_path):
    return payment_fixture(tmp_path)


def payment_page(fixture):
    return query_chat_history(
        fixture.root,
        source="wechat",
        account="account:" + fixture.primary["source_account_id"],
        session=fixture.session,
    )


def _payments(record):
    for child in record["items"]:
        if child.messaging.get("payment"):
            yield child.messaging["payment"]
        if child.messaging.get("forwarded_record"):
            yield from _payments(child.messaging["forwarded_record"])


def test_nested_payment_roles_roundtrip_as_native_app_messages(payment_store):
    fixture = payment_store
    for key in ("forwarded", "quoted"):
        parsed = fixture.parsed[key]
        assert (
            native_text_metadata(parsed, {})["text_components"]
            == parsed["text_components"]
        )
        provenance = {"message_type": 49, **parsed}
        before = deepcopy(provenance)
        details = list(
            native_details(
                provenance,
                parent_id=fixture.identifiers[key],
                account_id=fixture.primary["source_account_id"],
                peer_id=PEER,
            )
        )
        assert len(details) == (2 if key == "forwarded" else 1)
        assert all(item["original_message_type"] == 49 for item in details)
        assert all(item["sender_id"] == PEER for item in details)
        assert provenance == before
    assert fixture.history.read_bytes() == fixture.preserved


def test_payment_projection_is_typed_recursive_and_preserves_account_raw_exports(
    payment_store,
):
    fixture = payment_store
    page = payment_page(fixture)
    by_id = {item.stable_id: item for item in page.items}
    expected = {
        "cny-sent": ("transfer", "CNY", "sent"),
        "hkd-received": ("red_packet", "HKD", "received"),
        "unknown-received": ("transfer", "", "received"),
        "conflict-sent": ("red_packet", "", "sent"),
    }
    for key, (kind, currency, direction) in expected.items():
        message = by_id[fixture.identifiers[key]]
        observation = next(
            item for item in fixture.observations if item["extraction_record_id"] == key
        )
        payment = message.messaging["payment"]
        assert (payment["kind"], payment["currency"]) == (kind, currency)
        assert payment["source"] == "payment_card_fields"
        assert message.role == direction
        assert message.content_text == fixture.parsed[key]["text"]
        assert message.messaging["display_text"] == message.content_text
        assert message.author_label == observation["sender_display_name"]
        assert message.messaging["sender_id"] == observation["sender_id"]
        assert datetime.fromisoformat(message.last_seen_at) == datetime.fromisoformat(
            observation["sent_at_utc_iso"]
        )
    for key in ("prose", "other-app-subtype", "forwarded", "quoted"):
        assert not by_id[fixture.identifiers[key]].messaging.get("payment")
    forwarded = by_id[fixture.identifiers["forwarded"]].messaging["forwarded_record"]
    assert {(item["kind"], item["currency"]) for item in _payments(forwarded)} == {
        ("red_packet", "CNY"),
        ("transfer", "HKD"),
    }
    quoted = by_id[fixture.identifiers["quoted"]]
    quote = quoted.messaging["quoted_reply"]
    assert quote["payment"]["kind"] == "red_packet"
    assert quote["payment"]["currency"] == "HKD"
    assert quoted.messaging["display_text"] == REPLY
    assert len(page.items) == len(fixture.parsed)
    other_sessions = query_chat_history(
        fixture.root,
        source="wechat",
        account="account:" + fixture.other["source_account_id"],
        query="CNY",
    )
    assert len(other_sessions.sessions) == 1
    other = query_chat_history(
        fixture.root,
        source="wechat",
        account="account:" + fixture.other["source_account_id"],
        session=other_sessions.sessions[0].stable_id,
    )
    assert other.total_count == 1
    assert other.items[0].stable_id not in by_id
    markdown = build_chat_history_markdown(page)
    for parsed in fixture.parsed.values():
        assert parsed["text"].strip() in markdown
    response = fixture.app.test_client().get(
        f"/browser/wechat/session/{fixture.session}/export.csv",
        query_string={"account": "account:" + fixture.primary["source_account_id"]},
    )
    assert response.status_code == 200
    exported = list(csv.DictReader(StringIO(response.data.decode("utf-8-sig"))))
    assert len(exported) == len(fixture.parsed)
    assert {item["Statement"] for item in exported} == {
        item["text"] for item in fixture.parsed.values()
    }
    assert fixture.history.read_bytes() == fixture.preserved


def test_actual_payment_template_is_inert_escaped_and_uses_common_component(
    payment_store,
):
    fixture = payment_store
    response = get_verified_browser_page(
        fixture.app.test_client(),
        "/browser",
        query_string={
            "view": "text",
            "source": "wechat",
            "account": "account:" + fixture.primary["source_account_id"],
            "session": fixture.session,
        },
    )
    assert response.status_code == 200
    rendered = response.get_data(as_text=True)
    assert rendered.count('data-wechat-payment-kind="') == 7
    assert rendered.count('data-wechat-payment-currency="CNY"') == 2
    assert rendered.count('data-wechat-payment-currency="HKD"') == 3
    assert rendered.count('data-wechat-payment-currency=""') == 2
    assert rendered.count('class="wechat-message-text wechat-payment-text"') == 7
    assert (
        "&lt;script&gt;window.paymentScript=true&lt;/script&gt; &amp; snacks"
        in rendered
    )
    assert "<script>window.paymentScript=true</script>" not in rendered
    assert 'href="wxpay:' not in rendered
    assert "private-transfer-id" not in rendered
    assert fixture.history.read_bytes() == fixture.preserved


def test_fixed_payment_copy_is_localized_across_roles_without_rewriting_sources(
    tmp_path,
):
    fixture = localized_payment_fixture(tmp_path)
    page = payment_page(fixture)
    by_id = {item.stable_id: item for item in page.items}
    transfer = by_id[fixture.identifiers["cny-sent"]]
    assert transfer.messaging["payment"]["display_text"] == "\n".join(
        [
            "[Transfer]",
            "Transfer to you",
            "WeChat transfer",
            "CNY 1,234.50",
            fixture.literal_memo,
        ]
    )
    red_packet = by_id[fixture.identifiers["hkd-received"]]
    assert red_packet.messaging["payment"]["display_text"] == "\n".join(
        [
            "[Red packet]",
            "WeChat red packet",
            "HK$ 88.00",
            "Original source copy",
            fixture.literal_greeting,
        ]
    )
    unknown = by_id[fixture.identifiers["unknown-received"]]
    assert unknown.messaging["payment"]["display_text"] == unknown.content_text
    forwarded = by_id[fixture.identifiers["forwarded"]].messaging["forwarded_record"]
    embedded = list(_payments(forwarded))
    assert {item["display_text"] for item in embedded} == {
        "[Red packet]\nWeChat red packet\nCNY 12.50\nOriginal source copy",
        "[Transfer]\nWeChat transfer\nHK$ 88.00\nOriginal source copy",
    }
    quote = by_id[fixture.identifiers["quoted"]].messaging["quoted_reply"]
    assert quote["payment"]["display_text"] == (
        "[Red packet]\nWeChat red packet\nHKD 16.00\nOriginal source copy"
    )
    assert quote["text"].startswith("[Red packet]\n微信红包\n微信紅包\n")
    assert by_id[fixture.identifiers["quoted"]].messaging["display_text"] == REPLY
    assert (
        by_id[fixture.identifiers["prose"]].messaging["display_text"]
        == (fixture.parsed["prose"]["text"])
    )
    for key, parsed in fixture.parsed.items():
        assert by_id[fixture.identifiers[key]].content_text == parsed["text"]
    assert fixture.parsed["cny-sent"]["text"] == "\n".join(
        [
            "[Transfer]",
            "给你转账",
            "微信转账",
            "CNY 1,234.50",
            fixture.literal_memo,
        ]
    )
    markdown = build_chat_history_markdown(page)
    for parsed in fixture.parsed.values():
        assert parsed["text"].strip() in markdown
    response = fixture.app.test_client().get(
        f"/browser/wechat/session/{fixture.session}/export.csv",
        query_string={"account": "account:" + fixture.primary["source_account_id"]},
    )
    assert response.status_code == 200
    exported = list(csv.DictReader(StringIO(response.data.decode("utf-8-sig"))))
    assert {item["Statement"] for item in exported} == {
        item["text"] for item in fixture.parsed.values()
    }
    assert fixture.history.read_bytes() == fixture.preserved
