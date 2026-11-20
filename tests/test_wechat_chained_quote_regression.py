"""Encoded quoted reply bodies retain their exact available roles and source clocks.

Code version: v1.1.0-codex.0
"""

from copy import deepcopy
import hashlib
from html import escape
import json
import sqlite3

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_text
from app.core.wechat_reconcile import native_text_metadata
from tests import test_wechat_macos_recovery_bank_upgrades as fixtures


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive
app_stubs = fixtures.app_stubs
REPLY = "Current literal reply <b>unrendered</b> & next line\nSecond line"
QUOTED = "Earlier literal reply • a different speaker's own body"
REFERENCE = {
    "original_message_type": 49,
    "source_sender_id": "wxid_synthetic_quoted_speaker",
    "source_sender_label": "Earlier speaker",
    "source_chat_id": "wxid_synthetic_source_chat",
    "source_timestamp": "9007199254740993",
    "source_ids": {"svrid": "18446744073709551615"},
}


def _embedded(*, duplicate_title=False):
    title = "<title>" + escape(QUOTED) + "</title>"
    return (
        "<msg><appmsg>" + title + (title if duplicate_title else "")
        + "<type>57</type><appattach><totallen>0</totallen><attachid />"
        "</appattach></appmsg></msg>"
    )


def _payload(representation="escaped", *, duplicate_title=False):
    nested = _embedded(duplicate_title=duplicate_title)
    encoded = (
        "<![CDATA[" + nested + "]]>" if representation == "cdata"
        else nested if representation == "inline" else escape(nested)
    )
    return (
        "<msg><appmsg><title>" + escape(REPLY) + "</title><type>57</type>"
        "<appattach><totallen>0</totallen><attachid /></appattach><refermsg>"
        "<type>49</type><svrid>18446744073709551615</svrid>"
        "<fromusr>wxid_synthetic_quoted_speaker</fromusr>"
        "<chatusr>wxid_synthetic_source_chat</chatusr>"
        "<displayname>Earlier speaker</displayname><content>" + encoded
        + "</content><msgsource>&lt;msgsource /&gt;</msgsource>"
        "<createtime>9007199254740993</createtime></refermsg></appmsg>"
        "<fromusername>wxid_synthetic_outer_sender</fromusername>"
        "<scene>0</scene><appinfo><appname>Synthetic source application</appname>"
        "</appinfo><commenturl /></msg>"
    )


@pytest.mark.parametrize("representation", ("escaped", "cdata", "inline"))
def test_encoded_reply_in_quote_retains_top_body_author_clock_and_source_ids(
    representation,
):
    result = wechat_text.extract_wechat_text(49, _payload(representation))
    assert result["text"] == REPLY + "\n\n" + QUOTED
    assert result["text_extraction_kind"] == "quoted_reply"
    assert result["appmsg_subtype"] == 57
    assert result["quoted_reference"] == REFERENCE
    assert result["text_components"] == [
        {"kind": "reply_text", "text": REPLY},
        {
            "kind": "quoted_text", "text": QUOTED, **REFERENCE,
            "appmsg_subtype": 57, "text_extraction_kind": "quoted_reply",
            "flags": ["quoted_native_reply_body"],
        },
    ]
    assert native_text_metadata(result, {})["text_components"] == result["text_components"]
    # The embedded envelope has no refermsg. Its own body must not acquire a guessed
    # earlier speaker, timestamp, third quote, or independent conversation identity.
    assert "quoted_reference" not in result["text_components"][1]
    assert "text_components" not in result["text_components"][1]
    assert "wxid_synthetic_outer_sender" not in json.dumps(result)
    assert "Synthetic source application" not in json.dumps(result)


def test_encoded_quote_uses_the_shared_xml_byte_budget_without_losing_top_reply(
    monkeypatch,
):
    raw = _payload()
    monkeypatch.setattr(
        wechat_text, "MAX_XML_BYTES",
        len(raw.encode()) + len(_embedded().encode()) - 1,
    )
    result = wechat_text.extract_wechat_text(49, raw)
    assert result["text"] == REPLY
    assert result["text_components"] == [{"kind": "reply_text", "text": REPLY}]
    assert "quoted_non_text_content_omitted" in result["flags"]
    assert result["quoted_reference"] == REFERENCE


def test_ambiguous_inner_title_is_not_flattened_or_guessed():
    result = wechat_text.extract_wechat_text(49, _payload(duplicate_title=True))
    assert result["text"] == REPLY
    assert result["text_components"] == [{"kind": "reply_text", "text": REPLY}]
    assert "quoted_non_text_content_omitted" in result["flags"]


@pytest.mark.parametrize("clock_delta", (0, 1, -1, 2, -2))
def test_same_shape_app_stub_upgrade_records_bounded_dual_source_clocks(
    app_stubs, clock_delta
):
    fixture = app_stubs
    packet = deepcopy(fixture.packets[0])
    message = packet["messages"][0]
    raw = _payload()
    native = wechat_text.extract_wechat_text(49, raw)
    message.update(
        text=native["text"], native_text=native,
        content_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    )
    message["create_time"] += clock_delta
    before = {
        path: path.read_bytes()
        for path in (fixture.history, fixture.catalog, fixture.source)
    }
    original_id = fixture.originals[message["server_id"]]["id"]
    with sqlite3.connect(fixture.catalog) as connection:
        connection.row_factory = sqlite3.Row
        original = dict(connection.execute(
            "SELECT * FROM messages WHERE id=?", (original_id,)
        ).fetchone())
    candidate = fixture.history.with_name("encoded-quote-candidate.sqlite3")
    receipt = bank.stage_mac_recovery_bank(
        fixture.root, [packet], candidate, account_ids=(packet["account_id"],)
    )
    assert receipt["added"] == 0
    accepted = abs(clock_delta) <= 1
    assert receipt["upgraded"] == int(accepted)
    assert receipt["conflicts"] == int(not accepted)
    assert not fixture.fixture.bank_path.exists()
    assert all(path.read_bytes() == content for path, content in before.items())
    with sqlite3.connect(candidate) as connection:
        connection.row_factory = sqlite3.Row
        shadow = connection.execute("SELECT * FROM messages").fetchone()
        if not accepted:
            assert shadow is None
            assert connection.execute("SELECT COUNT(*) FROM gaps").fetchone()[0] == 1
        else:
            assert shadow["id"] == original_id
            for key in ("ordinal", "time_sort", "timestamp_value", "timestamp_label", "time_basis"):
                assert shadow[key] == original[key]
            assert shadow["text"] == REPLY + "\n\n" + QUOTED
            proof = json.loads(shadow["provenance"])
            assert proof["quoted_reference"] == REFERENCE
            assert proof["macos_recovery"]["create_time"] == message["create_time"]
            assert proof["unix_timestamp"] == message["create_time"]
            upgrade = proof["macos_stub_upgrade"]
            assert upgrade["original_id"] == original_id
            if clock_delta == 0:
                assert upgrade["upgrade_basis"] == "inert_resource_stub_to_authenticated_native_projection_v1"
                assert "clock_observations" not in upgrade
            else:
                assert upgrade["upgrade_basis"] == "inert_resource_stub_to_authenticated_native_projection_v2"
                assert upgrade["clock_observations"] == {
                    "catalog": {
                        "epoch": message["create_time"] - clock_delta,
                        **{key: original[key] for key in (
                            "time_sort", "timestamp_value", "timestamp_label", "time_basis",
                        )},
                    },
                    "native": {"epoch": message["create_time"]},
                    "native_minus_catalog_seconds": clock_delta,
                }
