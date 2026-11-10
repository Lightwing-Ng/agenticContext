"""Nontext quoted references remain typed and source-scoped without payload reads.

Code version: v1.3.2-codex.0
"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from xml.sax.saxutils import escape

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest

from app.core.wechat_history_browser import _native_quoted_reply_presentation
from app.core.wechat_forwarded import project_native_forwarded
from app.core.wechat_quoted_resources import (
    NESTED_REFERENCE_BASIS,
    ORIGINAL_BASIS,
    REFERENCE_BASIS,
    project_native_quoted_resources,
)
from app.core.wechat_resources import present_resource
from app.core.wechat_text import extract_wechat_text
from tests.test_wechat_quote_padding import _fixture as _padding_fixture
from tests.test_wechat_resources import _RenderedMarkup, _text


def _fixture(message_type=3, kind="image"):
    fixture = _padding_fixture()
    original, reply = fixture.original, fixture.reply
    original.content_text = "[Image]" if message_type == 3 else "[App message]"
    proof = original.messaging["provenance"]
    proof.update(
        message_type=message_type,
        text_extraction_kind="resource_placeholder",
        resource_reference={
            "kind": kind,
            "account_id": original.messaging["account_id"],
            "peer_id": original.messaging["peer_id"],
            "availability": "metadata_only",
            "unix_timestamp": proof["unix_timestamp"],
            "filename": "Source attachment.pdf"
            if kind == "file"
            else "https://untrusted.example/<script>.png",
        },
    )
    reference = reply.messaging["provenance"]["quoted_reference"]
    reference.update(
        original_message_type=message_type,
        source_timestamp=str(proof["unix_timestamp"]),
    )
    current = reply.messaging["provenance"]
    current["text_components"] = current["text_components"][:1]
    reply.content_text = current["text_components"][0]["text"]
    reply.messaging.update(_native_quoted_reply_presentation(current))
    fixture.items = [reply]
    fixture.originals = [original]
    return fixture


def _run(fixture):
    project_native_quoted_resources(
        fixture.items,
        fixture.names,
        fixture.accounts,
        fixture.snapshots,
        originals=fixture.originals,
    )
    return fixture.reply.messaging["quoted_reply"]


@pytest.mark.parametrize(
    ("message_type", "kind", "label"),
    [
        (3, "image", "Image"), (49, "app", "App message"),
        (49, "file", "File"),
    ],
)
def test_exact_original_adds_inert_typed_reference_without_copying_payloads(
    message_type, kind, label
):
    fixture = _fixture(message_type, kind)
    original = deepcopy(fixture.original)
    current = deepcopy(fixture.reply)
    with patch(
        "app.core.wechat_mac_resources.WechatMacResourceResolver.resolve"
    ) as resolve:
        quote = _run(fixture)
        resolve.assert_not_called()
    assert quote["resource_basis"] == ORIGINAL_BASIS
    assert quote["resource"]["label"] == label
    assert quote["resource"]["status"] == "metadata_only"
    assert not quote["resource"]["can_reveal"]
    assert quote["resource_original"] == {
        "message_id": fixture.original.stable_id,
        "server_id": fixture.original.messaging["provenance"]["server_id"],
        "source_snapshot": 26,
    }
    assert fixture.original == original
    assert fixture.reply.content_text == current.content_text
    assert fixture.reply.messaging["provenance"] == current.messaging["provenance"]
    assert quote["text"] == current.messaging["quoted_reply"]["text"]
    assert quote["reference"] == current.messaging["quoted_reply"]["reference"]
    assert "untrusted" not in str(quote["resource"])
    projected = deepcopy(quote)
    assert _run(fixture) == projected


@pytest.mark.parametrize(
    ("message_type", "kind"),
    [(3, "image"), (34, "audio"), (43, "video"), (47, "sticker"), (49, "app")],
)
def test_type_only_reference_does_not_claim_the_original_was_found(message_type, kind):
    fixture = _fixture(message_type, kind)
    fixture.originals = []
    quote = _run(fixture)
    assert quote["resource_basis"] == REFERENCE_BASIS
    assert quote["resource"]["kind"] == kind
    assert "resource_original" not in quote


@pytest.mark.parametrize("chat_role", ["original", "reply"])
@pytest.mark.parametrize("sender", ["wxid_padding_author", "legacy_native_original"])
def test_exact_group_card_accepts_only_original_proved_author_and_retains_labels(chat_role, sender):
    fixture = _fixture(49, "app")
    fixture.original.messaging["sender_id"] = sender
    fixture.original.messaging["provenance"]["resource_reference"].update(
        app_subtype="5", safe_title="Literal original title", publisher_name="Literal publisher"
    )
    reference = fixture.reply.messaging["provenance"]["quoted_reference"]
    reference["source_chat_id"] = sender if chat_role == "original" else fixture.reply.messaging["sender_id"]
    before, original_before = deepcopy(fixture.reply), deepcopy(fixture.original)
    quote = _run(fixture)
    assert quote["resource_basis"] == ORIGINAL_BASIS
    assert quote["resource"]["label"] == "Literal original title"
    assert quote["resource"]["publisher_name"] == "Literal publisher"
    assert fixture.original == original_before
    assert fixture.reply.messaging["provenance"] == before.messaging["provenance"]
    assert fixture.reply.content_text == before.content_text


@pytest.mark.parametrize("sender", ["789", "wxid_unknwn_123", "123@chatroom"])
def test_exact_original_does_not_promote_unresolved_native_author_values(sender):
    fixture = _fixture(49, "app")
    fixture.original.messaging["sender_id"] = sender
    fixture.reply.messaging["provenance"]["quoted_reference"]["source_chat_id"] = fixture.reply.messaging["sender_id"]
    quote = _run(fixture)
    assert quote["resource_basis"] == REFERENCE_BASIS
    assert "resource_original" not in quote


def _nested_fixture(*, quoted_container=False, grouped=False):
    fixture = _fixture()
    original_proof = fixture.original.messaging["provenance"]
    reference = (
        "<refermsg><type>3</type><fromusr>wxid_padding_author</fromusr>"
        "<chatusr>123@chatroom</chatusr><displayname>Quoted source</displayname>"
        f"<svrid>{original_proof['server_id']}</svrid>"
        f"<createtime>{original_proof['unix_timestamp']}</createtime></refermsg>"
    )
    children = (
        '<dataitem datatype="49" dataid="quoted-child">'
        "<sourcename>Forwarded reply author</sourcename><dataitemsource>"
        "<fromusr>wxid_padding_replier</fromusr></dataitemsource>"
        "<appmsg><type>57</type><title>Nested reply</title>"
        + reference + "</appmsg></dataitem>"
    )
    if grouped:
        children = (
            '<dataitem datatype="17"><recordinfo><datalist>'
            + children + "</datalist></recordinfo></dataitem>"
        )
    raw = (
        "<msg><appmsg><type>19</type><title>Source forwarding container</title>"
        "<recorditem><recordinfo><datalist>" + children
        + "</datalist></recordinfo></recorditem></appmsg></msg>"
    )
    if quoted_container:
        raw = (
            "<msg><appmsg><type>57</type><title>Outer reply</title>"
            "<refermsg><type>49</type><fromusr>wxid_forwarder</fromusr>"
            "<displayname>Source forwarder</displayname><content>"
            + escape(raw) + "</content></refermsg></appmsg></msg>"
        )
    parsed = extract_wechat_text(49, raw)
    assert parsed is not None
    parent = fixture.reply
    proof = parent.messaging["provenance"]
    proof.pop("quoted_reference", None)
    proof.update({key: value for key, value in parsed.items() if key not in {"text", "flags", "original_message_type"}})
    parent.content_text = parsed["text"]
    parent.messaging.pop("quoted_reply", None)
    record = project_native_forwarded(
        proof, parent_id=parent.stable_id, account_id=parent.messaging["account_id"]
    )
    assert record is not None
    if quoted_container:
        parent.messaging.update(_native_quoted_reply_presentation(proof))
        parent.messaging["quoted_reply"]["forwarded_record"] = record
    else:
        parent.messaging["forwarded_record"] = record
    child = record["items"][0]
    if grouped:
        child = child.messaging["forwarded_record"]["items"][0]
    fixture.parent, fixture.child, fixture.record = parent, child, record
    return fixture


def _run_nested(fixture):
    project_native_quoted_resources(
        fixture.items, fixture.names, fixture.accounts, fixture.snapshots,
        originals=fixture.originals,
    )
    return fixture.child.messaging["quoted_reply"]


@pytest.mark.parametrize("quoted_container", [False, True])
@pytest.mark.parametrize("grouped", [False, True])
def test_nested_native_quote_projects_only_source_bound_type_metadata(quoted_container, grouped):
    fixture = _nested_fixture(quoted_container=quoted_container, grouped=grouped)
    before = deepcopy(fixture.parent)
    with patch("app.core.wechat_mac_resources.WechatMacResourceResolver.resolve") as resolve:
        quote = _run_nested(fixture)
        resolve.assert_not_called()
    assert quote["resource_basis"] == NESTED_REFERENCE_BASIS
    assert quote["resource"] == present_resource({"kind": "image", "availability": "metadata_only"}, None)
    assert "resource_original" not in quote
    assert "viewer_display_name" not in quote
    assert fixture.parent.content_text == before.content_text
    assert fixture.parent.messaging["provenance"] == before.messaging["provenance"]
    assert fixture.child.messaging["quoted_reply"]["reference"] == quote["reference"]


@pytest.mark.parametrize(
    "defect", ["account", "snapshot", "source_database", "body", "path", "component", "reference", "html", "missing_proof"]
)
def test_nested_resource_cannot_borrow_another_source_path_or_html_tree(defect):
    fixture = _nested_fixture(grouped=True)
    parent, child = fixture.parent, fixture.child
    if defect == "account":
        fixture.snapshots[26]["metadata"]["account_hash_verified"] = False
    elif defect == "snapshot":
        parent.messaging["provenance"]["source_snapshot"] = 999
    elif defect == "source_database":
        parent.messaging["provenance"]["locator"]["database_sha256"] = "b" * 64
    elif defect == "body":
        parent.content_text += " changed"
    elif defect in {"path", "component"}:
        component = deepcopy(child.messaging["provenance"]["embedded_forwarded_component"])
        component["forwarded_item_path" if defect == "path" else "source_sender_id"] = [999] if defect == "path" else "wxid_another_source"
        child.messaging["provenance"]["embedded_forwarded_component"] = component
    elif defect == "reference":
        child.messaging["quoted_reply"]["reference"] = {"original_message_type": 3}
    elif defect == "html":
        fixture.record["authority"] = "source_verified_html_export_tree"
    else:
        child.messaging["provenance"] = None
    quote = _run_nested(fixture)
    assert "resource" not in quote and "resource_original" not in quote


def test_nested_resource_reprojection_removes_only_its_stale_source_claim():
    fixture = _nested_fixture()
    quote = _run_nested(fixture)
    before = deepcopy(quote)
    fixture.snapshots[26]["metadata"]["account_hash_verified"] = False
    quote = _run_nested(fixture)
    assert "resource" not in quote and "resource_basis" not in quote
    assert quote == {key: value for key, value in before.items() if key not in {"resource", "resource_basis"}}


@pytest.mark.parametrize(
    "defect",
    [
        "conversation",
        "account",
        "partition",
        "snapshot",
        "database_sha",
        "database_path",
        "table",
        "author",
        "server",
        "duplicate",
        "message_type",
        "later_row",
        "later_epoch",
        "reference_epoch",
        "reference_epoch_absent",
        "resource_account",
        "resource_peer",
        "resource_kind",
        "resource_epoch",
        "availability",
        "original_kind",
        "invalid_id",
    ],
)
def test_conflicting_or_ambiguous_originals_keep_only_the_type_reference(defect):
    fixture = _fixture()
    original = fixture.original
    proof = original.messaging["provenance"]
    if defect == "conversation":
        original.conversation_id = "wechat:" + "e" * 64
    elif defect in {"account", "partition"}:
        original.messaging["account_key" if defect == "account" else "partition_id"] = (
            "other"
        )
    elif defect == "snapshot":
        proof["source_snapshot"] = 27
    elif defect in {"database_sha", "database_path", "table"}:
        field = {
            "database_sha": "database_sha256",
            "database_path": "database_relative_path",
            "table": "table",
        }[defect]
        proof["locator"][field] = "other"
    elif defect == "author":
        original.messaging["sender_id"] = "wxid_other_author"
    elif defect == "server":
        proof["server_id"] = "1234"
    elif defect == "duplicate":
        fixture.originals.append(deepcopy(original))
    elif defect == "message_type":
        proof["message_type"] = 49
    elif defect == "later_row":
        proof["locator"].update(local_id=118, row_id=118)
    elif defect == "later_epoch":
        proof["unix_timestamp"] = 1729084004
    elif defect in {"reference_epoch", "reference_epoch_absent"}:
        reference = fixture.reply.messaging["provenance"]["quoted_reference"]
        reference["source_timestamp"] = (
            "1729083984" if defect == "reference_epoch" else ""
        )
    elif defect.startswith("resource_") or defect == "availability":
        field = {
            "resource_account": "account_id",
            "resource_peer": "peer_id",
            "resource_kind": "kind",
            "resource_epoch": "unix_timestamp",
            "availability": "availability",
        }[defect]
        proof["resource_reference"][field] = "other"
    elif defect == "original_kind":
        proof["text_extraction_kind"] = "plain_text"
    elif defect == "invalid_id":
        original.stable_id = "<script>"
    quote = _run(fixture)
    assert quote["resource_basis"] == REFERENCE_BASIS
    assert quote["resource"]["kind"] == "image"
    assert "resource_original" not in quote


@pytest.mark.parametrize(
    "defect",
    [
        "account_proof",
        "server",
        "body",
        "reference",
        "quoted_text",
        "text_type",
        "payment",
        "forwarded",
        "huge_page",
        "huge_originals",
    ],
)
def test_unverified_or_complete_quotes_do_not_gain_a_resource_placeholder(defect):
    fixture = _fixture()
    proof = fixture.reply.messaging["provenance"]
    quote = fixture.reply.messaging["quoted_reply"]
    if defect == "account_proof":
        fixture.snapshots[26]["metadata"]["account_hash_verified"] = False
    elif defect == "server":
        proof["server_id"] = "0"
    elif defect == "body":
        fixture.reply.content_text += " other"
    elif defect == "reference":
        quote["reference"] = {"original_message_type": 3}
    elif defect == "quoted_text":
        proof["text_components"].append(
            {"kind": "quoted_text", "text": "retained", **proof["quoted_reference"]}
        )
    elif defect == "text_type":
        proof["quoted_reference"]["original_message_type"] = 1
    elif defect in {"payment", "forwarded"}:
        quote["payment" if defect == "payment" else "forwarded_record"] = {
            "kind": "retained"
        }
    elif defect == "huge_page":
        fixture.items *= 1_001
    elif defect == "huge_originals":
        fixture.originals *= 1_001
    assert "resource" not in _run(fixture)


@pytest.mark.parametrize(
    ("message_type", "kind", "label"),
    [
        (3, "image", "Image"), (34, "audio", "Voice message"), (43, "video", "Video"),
        (47, "sticker", "Sticker"), (49, "app", "App message"),
        (49, "file", "File"),
    ],
)
def test_actual_quote_fragment_reuses_the_inert_resource_row_without_active_urls(
    message_type, kind, label
):
    fixture = _fixture(message_type, kind)
    quote = _run(fixture)
    environment = Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=select_autoescape(["html"]),
    )
    module = environment.get_template("_wechat_browser.html").make_module(
        {"text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=[])}
    )
    markup = _RenderedMarkup(str(module.message_row(fixture.reply)))
    blockquote = markup.find(tag="blockquote", css_class="wechat-message-quote")
    resource = markup.find(css_class="wechat-resource", within=blockquote)
    assert resource.attrs["data-wechat-resource-kind"] == kind
    assert resource.attrs["data-wechat-resource-status"] == "metadata_only"
    assert _text(markup.find(css_class="wechat-resource-label", within=resource)) == label
    assert _text(markup.find(css_class="wechat-resource-status", within=resource)) == "Metadata only"
    assert not any(
        element.tag in {"a", "img", "video", "audio", "button", "script"}
        and any(parent is resource for parent in element.ancestors)
        for element in markup.elements
    )
    assert "data-wechat-resource-reveal" not in resource.attrs
    assert _text(markup.find(css_class="wechat-message-text")) == fixture.reply.content_text
    assert "unavailable" not in _text(resource)
    ordinary = _RenderedMarkup(str(module.resource_card(quote["resource"])))
    original_row = ordinary.find(css_class="wechat-resource")
    assert original_row.attrs == resource.attrs
    assert _text(original_row) == _text(resource)
    assert (
        present_resource(
            fixture.original.messaging["provenance"]["resource_reference"], None
        )
        == quote["resource"]
    )


def test_reprojection_drops_only_its_stale_original_claim():
    fixture = _fixture()
    _run(fixture)
    fixture.originals = []
    quote = _run(fixture)
    assert quote["resource_basis"] == REFERENCE_BASIS
    assert "resource_original" not in quote


def test_legacy_ios_reference_uses_an_inert_quote_copy_and_preserves_its_resolver():
    fixture = _fixture()
    original = fixture.original
    reference = original.messaging["provenance"]["resource_reference"]
    reference.pop("availability")
    before = deepcopy(original)
    quote = _run(fixture)
    assert quote["resource_basis"] == ORIGINAL_BASIS
    assert quote["resource_original"]["message_id"] == original.stable_id
    assert quote["resource"]["status"] == "metadata_only"
    assert not quote["resource"]["can_reveal"]
    assert original == before and "availability" not in reference
    resolver = SimpleNamespace(
        resolve=lambda value: {
            "status": "folder",
            "label": "Mac folder",
            "path": "/private/retained-resource",
        }
    )
    assert present_resource(reference, resolver)["status"] == "folder"
    assert present_resource(reference, resolver)["can_reveal"]


@pytest.mark.parametrize(
    "defect",
    ["snapshot", "database_sha", "database_path", "author", "null_availability"],
)
def test_legacy_availability_adapter_keeps_existing_scope_and_actor_guards(defect):
    fixture = _fixture()
    proof = fixture.original.messaging["provenance"]
    proof["resource_reference"].pop("availability")
    if defect == "snapshot":
        proof["source_snapshot"] = 99
    elif defect in {"database_sha", "database_path"}:
        proof["locator"][
            "database_sha256" if defect == "database_sha" else "database_relative_path"
        ] = "unverified"
    elif defect == "author":
        fixture.original.messaging["sender_id"] = "wxid_another_actor"
    else:
        proof["resource_reference"]["availability"] = None
    quote = _run(fixture)
    assert quote["resource_basis"] == REFERENCE_BASIS
    assert "resource_original" not in quote
