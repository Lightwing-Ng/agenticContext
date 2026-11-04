"""Only an exact retained native original can remove quote display padding.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import hashlib
from types import SimpleNamespace

from bs4 import BeautifulSoup
import pytest

from app.core.wechat_history_browser import _native_quoted_reply_presentation
from app.core.wechat_quote_padding import MATCH_BASIS, project_native_quote_padding
from app.core.wechat_text import native_components_text


OWNER = "wxid_padding_owner"
AUTHOR = "wxid_padding_author"
PEER = "123@chatroom"
SOURCE = "ios-" + hashlib.md5(OWNER.encode(), usedforsecurity=False).hexdigest()
DATABASE = "message_4.sqlite"
PATH = "Documents/" + SOURCE[4:] + "/DB/" + DATABASE
SHA = "a" * 64
BODY = "First quoted line\n\n  Indented line  "
REPLY = "The reply\nA second reply line"
SERVER = "1691959108633843536"


def _fixture(prefix="\n", *, body=BODY):
    locator = {
        "database": DATABASE,
        "database_relative_path": PATH,
        "database_sha256": SHA,
        "table": "Chat_"
        + hashlib.md5(PEER.encode(), usedforsecurity=False).hexdigest(),
        "local_id": 115,
        "row_id": 115,
    }
    common = {
        "account_key": "account:" + OWNER,
        "account_id": OWNER,
        "partition_id": SOURCE,
        "peer_id": PEER,
        "direction": "received",
        "kind": "group",
    }
    original = SimpleNamespace(
        stable_id="b" * 64,
        conversation_id="wechat:" + "c" * 64,
        content_text=body,
        messaging={
            **common,
            "sender_id": AUTHOR,
            "provenance": {
                "message_type": 1,
                "text_extraction_kind": "plain_text",
                "server_id": SERVER,
                "source_snapshot": 26,
                "unix_timestamp": 1729083981,
                "locator": locator,
            },
        },
    )
    reference = {
        "original_message_type": 1,
        "source_sender_id": PEER,
        "source_chat_id": AUTHOR,
        "source_sender_label": "Quoted source label",
        "source_timestamp": "1729083984",
        "source_ids": {"svrid": SERVER},
    }
    components = [
        {"kind": "reply_text", "text": REPLY},
        {"kind": "quoted_text", "text": prefix + body, **reference},
    ]
    provenance = {
        "message_type": 49,
        "text_extraction_kind": "quoted_reply",
        "appmsg_subtype": 57,
        "source_snapshot": 26,
        "server_id": "6346625499473082965",
        "unix_timestamp": 1729084003,
        "quoted_reference": reference,
        "text_components": components,
        "locator": {**locator, "local_id": 117, "row_id": 117},
    }
    reply = SimpleNamespace(
        stable_id="d" * 64,
        conversation_id=original.conversation_id,
        content_text=native_components_text(components),
        author_label="Reply sender",
        last_seen_at="2024-10-16T21:06:43+08:00",
        messaging={
            **common,
            "sender_id": "wxid_padding_replier",
            "provenance": provenance,
            "timestamp_label": "16 Oct 2024 21:06:43",
            "time_basis": "UTC+08:00",
            "sender_identifier_namespace": "native_username",
            "contact": {},
            **_native_quoted_reply_presentation(provenance),
        },
    )
    snapshots = {
        26: {
            "source_id": SOURCE,
            "snapshot_id": "native-snapshot",
            "metadata": {
                "source_id": SOURCE,
                "source_account_id": OWNER,
                "account_hash_verified": True,
                "account_hash": SOURCE[4:],
                "input_databases": [{"path": PATH, "sha256": SHA}],
            },
        }
    }
    accounts = [{"partition_id": SOURCE, "key": "account:" + OWNER, **common}]
    names = SimpleNamespace(account_owner=lambda context: context.get("account_id", ""))
    return SimpleNamespace(
        items=[original, reply],
        original=original,
        reply=reply,
        snapshots=snapshots,
        accounts=accounts,
        names=names,
    )


def _run(fixture):
    project_native_quote_padding(
        fixture.items, fixture.names, fixture.accounts, fixture.snapshots
    )
    return fixture.reply.messaging["quoted_reply"]


@pytest.mark.parametrize("prefix", ["\n", "\r", "\r\n", "\n\r\n\n"])
def test_exact_original_removes_only_extra_display_linebreaks(prefix):
    fixture = _fixture(prefix)
    before = deepcopy(fixture.items)
    quote = _run(fixture)
    assert quote["display_text"] == BODY
    assert quote["text"] == prefix + BODY
    assert quote["display_text_basis"] == MATCH_BASIS
    assert quote["display_original_id"] == fixture.original.stable_id
    assert fixture.original == before[0]
    assert fixture.reply.content_text == before[1].content_text
    assert fixture.reply.messaging["provenance"] == before[1].messaging["provenance"]
    assert (
        fixture.reply.messaging["timestamp_label"]
        == before[1].messaging["timestamp_label"]
    )
    assert quote["reference"]["source_timestamp"] == "1729083984"
    assert fixture.original.messaging["provenance"]["unix_timestamp"] == 1729083981
    again = deepcopy(quote)
    assert _run(fixture) == again


def test_historical_ipad_snapshot_without_database_inventory_uses_exact_locator_pair():
    fixture = _fixture()
    del fixture.snapshots[26]["metadata"]["input_databases"]
    fixture.snapshots[26]["metadata"].update(
        snapshot_id="native-snapshot", device_product_type="iPad16,3"
    )
    assert _run(fixture)["display_text"] == BODY


@pytest.mark.parametrize("server", ["0", 123, True, "-123", "9" * 30, None])
def test_invalid_current_reply_server_is_not_a_verified_native_event(server):
    fixture = _fixture()
    fixture.reply.messaging["provenance"]["server_id"] = server
    assert "display_text" not in _run(fixture)


@pytest.mark.parametrize(
    "defect",
    [
        "original_absent",
        "body",
        "body_leading_blank",
        "conversation",
        "account",
        "partition",
        "snapshot",
        "database_sha",
        "database_path",
        "table",
        "server",
        "server_integer",
        "server_zero",
        "duplicate",
        "original_type",
        "original_kind",
        "later_row",
        "boolean_row",
        "conflicting_epoch",
        "author",
        "reference_author",
        "reference_type",
        "raw_body",
        "components",
        "snapshot_metadata",
        "snapshot_input",
        "unknown_scope",
        "plain_quote",
        "padding_space",
        "no_padding",
        "only_padding",
        "huge_page",
    ],
)
def test_missing_conflicting_or_intentional_padding_stays_literal(defect):
    f = _fixture()
    o, q = f.original, f.reply
    proof = o.messaging["provenance"]
    quote = q.messaging["quoted_reply"]
    if defect == "original_absent":
        f.items.remove(o)
    elif defect == "body":
        o.content_text += "!"
    elif defect == "body_leading_blank":
        o.content_text = "\n" + BODY
    elif defect in {"conversation", "account", "partition", "snapshot"}:
        if defect == "conversation":
            o.conversation_id = "wechat:" + "e" * 64
        elif defect == "snapshot":
            proof["source_snapshot"] = 27
        else:
            o.messaging["account_key" if defect == "account" else "partition_id"] = (
                "other"
            )
    elif defect in {"database_sha", "database_path", "table"}:
        proof["locator"][
            {
                "database_sha": "database_sha256",
                "database_path": "database_relative_path",
                "table": "table",
            }[defect]
        ] = "other"
    elif defect.startswith("server"):
        proof["server_id"] = {
            "server": "111",
            "server_integer": int(SERVER),
            "server_zero": "0",
        }[defect]
    elif defect == "duplicate":
        duplicate = deepcopy(o)
        duplicate.stable_id = "e" * 64
        f.items.append(duplicate)
    elif defect == "original_type":
        proof["message_type"] = 49
    elif defect == "original_kind":
        proof["text_extraction_kind"] = "resource_placeholder"
    elif defect == "later_row":
        proof["locator"].update(local_id=118, row_id=118)
    elif defect == "boolean_row":
        proof["locator"].update(local_id=True, row_id=True)
    elif defect == "conflicting_epoch":
        proof["unix_timestamp"] += 100
    elif defect == "author":
        o.messaging["sender_id"] = "wxid_otherauthor"
    elif defect == "reference_author":
        quote["reference"]["source_chat_id"] = "wxid_otherauthor"
    elif defect == "reference_type":
        quote["reference"]["original_message_type"] = 49
    elif defect == "raw_body":
        q.content_text += "!"
    elif defect == "components":
        q.messaging["provenance"]["text_components"].append(
            {"kind": "reply_text", "text": "Other"}
        )
    elif defect == "snapshot_metadata":
        f.snapshots[26]["metadata"]["account_hash_verified"] = False
    elif defect == "snapshot_input":
        f.snapshots[26]["metadata"]["input_databases"][0]["sha256"] = "f" * 64
    elif defect == "unknown_scope":
        o.messaging["account_key"] = {}
    elif defect == "plain_quote":
        q.messaging["provenance"]["message_type"] = 1
    elif defect in {"padding_space", "no_padding", "only_padding"}:
        quote["text"] = {
            "padding_space": " \n" + BODY,
            "no_padding": BODY,
            "only_padding": "\n\n",
        }[defect]
    elif defect == "huge_page":
        f.items *= 501
    assert "display_text" not in _run(f)


def test_actual_fragment_uses_only_display_override_and_escapes_its_copy():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    fixture = _fixture(body="<script>unsafe()</script>\n  second line  ")
    quote = _run(fixture)
    template = Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=select_autoescape(["html"]),
    ).get_template("_wechat_browser.html")
    module = template.make_module(
        {"text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=[])}
    )
    markup = str(module.message_row(fixture.reply))
    soup = BeautifulSoup(markup, "html.parser")
    node = soup.select_one(".wechat-quoted-text")
    assert node.get_text() == quote["display_text"]
    assert node.find("script") is None
    assert quote["text"].startswith("\n")
    del quote["display_text"]
    fallback = BeautifulSoup(str(module.message_row(fixture.reply)), "html.parser")
    assert fallback.select_one(".wechat-quoted-text").get_text() == quote["text"]
