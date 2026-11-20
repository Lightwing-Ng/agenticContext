"""Sealed Mac recovery reuses native presentation without borrowing iPhone provenance.

Code version: v1.5.2-codex.0
"""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest

from app.core import wechat_macos_recovery as recovery
from app.core.wechat_contacts import (
    WechatContactRepository,
    canonical_contact_id,
    contact_registry_path,
    initialize_contact_registry,
)
from app.core.wechat_history_browser import (
    _MACOS_RECOVERY_PRESENTATION,
    _macos_recovery_source_context,
    _native_quoted_reply_presentation,
    _present_macos_recovery_quotes,
    _resolve_macos_recovery_quoted_contacts,
    query_wechat_history,
)
from app.core.wechat_forwarded import project_native_forwarded
from app.core.wechat_reconcile import _TEXT_METADATA_FIELDS
from app.core.wechat_resources import present_resource
from app.core.wechat_text import extract_wechat_text, native_components_text
from app.web.presentation import format_wechat_source_clock
from tests import test_wechat_macos_recovery as recovery_fixtures
from tests.test_wechat_contacts import bind, insert_contact, observe
from tests.test_wechat_macos_recovery import OWNER, PEER
from tests.test_wechat_quoted_resources import _nested_fixture, _run_nested


mac_archive = recovery_fixtures.mac_archive


@pytest.fixture
def mac_page(mac_archive):
    root, history, session, packet = mac_archive
    with sqlite3.connect(history) as archive:
        source = archive.execute(
            "SELECT id FROM partitions WHERE account_id=?", (OWNER,),
        ).fetchone()[0]
        archive.execute(
            "UPDATE partitions SET account_label=? WHERE id=?",
            (f"Synthetic account owner · {OWNER[-6:]}", source),
        )
    original = history.read_bytes()
    recovery.publish_mac_recovery(root, recovery.stage_mac_recovery(root, packet))
    path = contact_registry_path(root)
    with sqlite3.connect(path) as connection:
        initialize_contact_registry(connection)
        for sender, name in ((OWNER, "Synthetic account owner"), (PEER, "Canonical peer")):
            contact = insert_contact(connection, sender)
            bind(connection, contact, source=source, sender=sender)
            observe(
                connection, contact, "remark", name, source=source,
                snapshot="", owner=OWNER,
            )
    with patch("app.core.wechat_mac_resources.WechatMacResourceResolver.resolve") as resolver:
        page = query_wechat_history(root, session=session, persist_event_cache=False)
        resolver.assert_not_called()
    assert history.read_bytes() == original
    recovered = [item for item in page.items if "macos_recovery" in item.messaging["provenance"]]
    reply = next(item for item in recovered if item.messaging["provenance"]["message_type"] == 49)
    image = next(item for item in recovered if item.messaging["provenance"]["server_id"] == "210")
    return SimpleNamespace(
        root=root, history=history, session=session, packet=packet, page=page,
        recovered=recovered, reply=reply, image=image,
    )


def test_mac_messages_keep_exact_source_and_canonical_sender_names(mac_page):
    fixture = mac_page
    assert len(fixture.recovered) == 5
    assert fixture.page.total_count == fixture.page.current_session.message_count == 7
    for item in fixture.recovered:
        context = item.messaging
        proof = context["provenance"]
        assert context["source_file"] == (
            "WeChat for Mac local database: db_storage/message/message_0.db"
        )
        assert context["source_label"] == "WeChat for Mac local database"
        assert context["source_snapshot_id"] == "synthetic-sealed-snapshot"
        assert context["backup_id"] == ""
        assert "source_snapshot" not in proof
        assert context["partition_id"].startswith("ios-")
        assert context["sender_identifier_namespace"] == "native_user_name"
        assert context["contact"]["identity_status"] == "verified"
        assert context["viewer_display_name"] == (
            "Synthetic account owner" if item.role == "sent" else "Canonical peer"
        )
        assert context["contact"]["viewer_display_name_source"] == (
            "verified_native_account_alias"
            if item.role == "sent"
            else "verified_native_contact_profile"
        )
        assert proof["locator"]["database_relative_path"] == "db_storage/message/message_0.db"
        assert "database_sha256" not in proof["locator"]
    anchors = [item for item in fixture.page.items if item not in fixture.recovered]
    assert all(item.messaging["source_file"] == "synthetic" for item in anchors)
    quote = fixture.reply.messaging["quoted_reply"]
    assert fixture.reply.content_text == fixture.reply.messaging["display_text"] == "Reply kept exactly"
    assert quote["viewer_display_name"] == "Canonical peer"
    assert quote["resource"]["kind"] == "image"
    assert quote["resource"]["status"] == "metadata_only"
    assert quote["resource_original"] == {
        "message_id": fixture.image.stable_id,
        "server_id": "210",
        "source_snapshot_id": "synthetic-sealed-snapshot",
    }


def test_rendered_mac_quote_keeps_reply_and_inert_typed_resource(mac_page):
    environment = Environment(
        loader=FileSystemLoader("app/web/templates"), autoescape=select_autoescape(["html"])
    )
    environment.filters["wechat_source_clock"] = format_wechat_source_clock
    module = environment.get_template("_wechat_browser.html").make_module(
        {"text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=[])}
    )
    soup = BeautifulSoup(str(module.message_row(mac_page.reply)), "html.parser")
    assert soup.select_one(".wechat-message-text").get_text() == "Reply kept exactly"
    assert soup.select_one(".wechat-quoted-speaker").get_text() == "Canonical peer"
    resource = soup.select_one("blockquote .wechat-resource")
    assert resource["data-wechat-resource-kind"] == "image"
    assert resource["data-wechat-resource-status"] == "metadata_only"
    assert not resource.find_all(["a", "img", "video", "audio", "button", "script"])
    assert not resource.has_attr("data-wechat-resource-reveal")
    source = soup.select_one(".wechat-provenance-fields").get_text()
    assert "WeChat for Mac local database" in source
    assert "db_storage/message/message_0.db" in source
    assert "iPhone" not in source and "Documents/" not in source


def test_stored_assertions_cannot_cross_runtime_validation_boundary(mac_page):
    item = mac_page.reply
    proof = item.messaging["provenance"]
    row = {"provenance": json.dumps(proof), "_macos_recovery_validated": True}
    assert _macos_recovery_source_context(row) == {}
    item.messaging["_macos_recovery_validated"] = True
    for field in ("resource", "resource_basis", "resource_original"):
        item.messaging["quoted_reply"].pop(field, None)
    _present_macos_recovery_quotes([item], originals=[mac_page.image])
    assert "resource" not in item.messaging["quoted_reply"]


@pytest.mark.parametrize(
    "change",
    [
        "source_id", "snapshot_id", "main_sha256", "wal_sha256", "database_relative_path",
        "table", "peer_id", "account_id", "server_id", "sender", "local_id", "create_time",
        "native_time", "kind", "type", "duplicate", "missing", "untrusted",
    ],
)
def test_mac_quote_links_only_one_exact_authenticated_original(mac_page, change):
    reply, original = mac_page.reply, mac_page.image
    receipt = original.messaging["_macos_recovery_receipt"]
    proof = original.messaging["provenance"]
    originals = [original]
    if change in {
        "source_id", "snapshot_id", "main_sha256", "wal_sha256", "database_relative_path",
        "table", "peer_id", "account_id", "server_id",
    }:
        receipt[change] = "different"
        proof["macos_recovery"][change] = "different"
    elif change == "sender":
        original.messaging["sender_id"] = OWNER
    elif change in {"local_id", "create_time"}:
        receipt[change] = reply.messaging["_macos_recovery_receipt"][change] + 1
        proof["macos_recovery"][change] = receipt[change]
    elif change in {"native_time", "kind"}:
        proof["resource_reference"][change] = "different"
    elif change == "type":
        proof["message_type"] = 43
    elif change == "duplicate":
        duplicate = deepcopy(original)
        duplicate.messaging["_macos_recovery_validated"] = _MACOS_RECOVERY_PRESENTATION
        originals.append(duplicate)
    elif change == "missing":
        originals = []
    else:
        original.messaging["_macos_recovery_validated"] = True
    _present_macos_recovery_quotes([reply], originals=originals)
    quote = reply.messaging["quoted_reply"]
    assert quote["resource_basis"] == "authenticated_macos_nontext_quote_type_v1"
    assert quote["resource"]["kind"] == "image"
    assert "resource_original" not in quote


def test_mac_quote_keeps_exact_original_outside_the_visible_page(mac_page):
    page = query_wechat_history(
        mac_page.root, session=mac_page.session, query="Reply kept", persist_event_cache=False
    )
    assert page.total_count == len(page.items) == 1
    assert page.items[0].messaging["quoted_reply"]["resource_original"]["message_id"] == mac_page.image.stable_id


def _app_quote(mac_page, kind="app"):
    reply = mac_page.reply
    original = replace(
        mac_page.image, content_text="[App message]" if kind == "app" else "[File]"
    )
    proof = original.messaging["provenance"]
    proof["message_type"] = 49
    for receipt in (
        proof["macos_recovery"],
        original.messaging["_macos_recovery_receipt"],
    ):
        receipt["local_type"] = ((4 if kind == "app" else 6) << 32) | 49
    proof["resource_reference"].update(
        kind=kind,
        app_subtype="4" if kind == "app" else "6",
        safe_title="A literal <b>card</b> title",
        publisher_name="Synthetic publisher",
    )
    for reference in (
        reply.messaging["provenance"]["quoted_reference"],
        reply.messaging["quoted_reply"]["reference"],
    ):
        reference["original_message_type"] = 49
    for field in ("resource", "resource_basis", "resource_original"):
        reply.messaging["quoted_reply"].pop(field, None)
    return reply, original


def test_mac_app_quote_expands_its_exact_original_forwarded_record(mac_page):
    reply, original = _app_quote(mac_page)
    proof = original.messaging["provenance"]
    proof.pop("resource_reference")
    components = [
        {"kind": "forwarded_text", "text": "Forwarded original line",
         "source_sender_label": "Native speaker"}
    ]
    proof.update(text_extraction_kind="forwarded_text", appmsg_subtype=19,
                 original_message_type=49, text_components=components)
    original = replace(original, content_text=native_components_text(components))
    for receipt in (
        proof["macos_recovery"], original.messaging["_macos_recovery_receipt"],
    ):
        receipt["local_type"] = (19 << 32) | 49
    quote = reply.messaging["quoted_reply"]
    for reference in (reply.messaging["provenance"]["quoted_reference"], quote["reference"]):
        reference["source_timestamp"] = str(proof["macos_recovery"]["create_time"])

    _present_macos_recovery_quotes([reply], originals=[original])

    record = quote["forwarded_record"]
    assert record["authority"] == "native_forwarded_envelope"
    assert [item.content_text for item in record["items"]] == ["Forwarded original line"]
    assert "resource" not in quote

    _present_macos_recovery_quotes([reply], originals=[])

    assert "forwarded_record" not in quote
    assert quote["resource"]["label"] == "App message"


def _inline_mac_app_quote(mac_page):
    reply, original = _app_quote(mac_page)
    proof = reply.messaging["provenance"]
    proof["quoted_reference"]["app_card_metadata"] = {
        "schema_version": 1, "appmsg_subtype": 76,
        "safe_title": "广岛之恋 <b>literal</b>",
        "app_name": "QQ音乐", "app_name_source": "appinfo/appname",
    }
    reply.messaging.update(_native_quoted_reply_presentation(proof))
    original_proof = original.messaging["provenance"]
    original_proof["resource_reference"].update(
        app_subtype="76", safe_title="广岛之恋 <b>literal</b>",
    )
    for receipt in (original_proof["macos_recovery"], original.messaging["_macos_recovery_receipt"]):
        receipt["local_type"] = (76 << 32) | 49
    return reply, original


def test_mac_inline_music_card_without_original_preserves_native_roles_and_literal_text(mac_page):
    reply, _original = _inline_mac_app_quote(mac_page)
    before = deepcopy((reply.content_text, reply.messaging["provenance"]))
    with patch("app.core.wechat_mac_resources.WechatMacResourceResolver.resolve") as resolver:
        _present_macos_recovery_quotes([reply])
        resolver.assert_not_called()
    quote = reply.messaging["quoted_reply"]
    assert quote["resource_basis"] == "authenticated_macos_inline_quoted_app_card_v1"
    assert quote["resource"]["label"] == "广岛之恋 <b>literal</b>"
    assert quote["resource"]["publisher_name"] == "QQ音乐"
    assert quote["resource"]["status"] == "metadata_only"
    assert quote["resource"]["can_reveal"] is False
    assert "resource_original" not in quote
    assert (reply.content_text, reply.messaging["provenance"]) == before
    first = deepcopy(quote)
    _present_macos_recovery_quotes([reply])
    assert reply.messaging["quoted_reply"] == first
    environment = Environment(loader=FileSystemLoader("app/web/templates"), autoescape=select_autoescape(["html"]))
    environment.filters["wechat_source_clock"] = format_wechat_source_clock
    module = environment.get_template("_wechat_browser.html").make_module({"text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=[])})
    soup = BeautifulSoup(str(module.message_row(reply)), "html.parser")
    assert soup.select_one(".wechat-resource-label").get_text() == "广岛之恋 <b>literal</b>"
    assert not soup.select(".wechat-resource b, .wechat-resource a, .wechat-resource button")


@pytest.mark.parametrize("conflict", [None, "safe_title", "app_name", "app_subtype", "kind"])
def test_mac_inline_card_uses_exact_original_only_when_card_facts_agree(mac_page, conflict):
    reply, original = _inline_mac_app_quote(mac_page)
    resource = original.messaging["provenance"]["resource_reference"]
    if conflict == "kind":
        resource["kind"] = "file"
    elif conflict is not None:
        resource[conflict] = "Conflicting source value"
    _present_macos_recovery_quotes([reply], originals=[original])
    quote = reply.messaging["quoted_reply"]
    if conflict is None:
        assert quote["resource_basis"] == "exact_authenticated_macos_resource_original_v1"
        assert quote["resource"]["label"] == "广岛之恋 <b>literal</b>"
        assert quote["resource"]["publisher_name"] == "Synthetic publisher"
        assert quote["resource_original"]["message_id"] == original.stable_id
    else:
        assert quote["resource_basis"] == "authenticated_macos_nontext_quote_type_v1"
        assert quote["resource"]["label"] == "App message"
        assert not quote["resource"].get("publisher_name")
        assert "resource_original" not in quote


@pytest.mark.parametrize("defect", ["account", "peer", "untrusted", "reference", "metadata"])
def test_mac_inline_card_projection_is_removed_when_its_authentication_changes(mac_page, defect):
    reply, _original = _inline_mac_app_quote(mac_page)
    _present_macos_recovery_quotes([reply])
    assert "resource" in reply.messaging["quoted_reply"]
    if defect in {"account", "peer"}:
        reply.messaging[defect + "_id"] = "different"
    elif defect == "untrusted":
        reply.messaging["_macos_recovery_validated"] = True
    elif defect == "reference":
        reply.messaging["quoted_reply"]["reference"] = {}
    else:
        reply.messaging["provenance"]["quoted_reference"]["app_card_metadata"]["url"] = "https://example.invalid/private"
    _present_macos_recovery_quotes([reply])
    assert "resource" not in reply.messaging["quoted_reply"]
    assert "resource_original" not in reply.messaging["quoted_reply"]


@pytest.mark.parametrize("quoted_container", [False, True])
@pytest.mark.parametrize("grouped", [False, True])
def test_mac_nested_music_quote_reuses_only_its_authenticated_source_component(mac_page, quoted_container, grouped):
    nested = _nested_fixture(quoted_container=quoted_container, grouped=grouped, app_card=True)
    parent = replace(mac_page.reply, content_text=nested.parent.content_text)
    proof = parent.messaging["provenance"]
    nested_proof = nested.parent.messaging["provenance"]
    for field in _TEXT_METADATA_FIELDS:
        proof.pop(field, None)
        if field in nested_proof:
            proof[field] = deepcopy(nested_proof[field])
    proof["text_extraction_kind"] = nested_proof["text_extraction_kind"]
    record = project_native_forwarded(proof, parent_id=parent.stable_id, account_id=OWNER)
    assert record is not None
    parent.messaging.pop("quoted_reply", None)
    if quoted_container:
        parent.messaging.update(_native_quoted_reply_presentation(proof))
        parent.messaging["quoted_reply"]["forwarded_record"] = record
    else:
        parent.messaging["forwarded_record"] = record
    child = record["items"][0]
    if grouped:
        child = child.messaging["forwarded_record"]["items"][0]
    before = deepcopy((parent.content_text, proof, child.messaging["provenance"]))
    with patch("app.core.wechat_mac_resources.WechatMacResourceResolver.resolve") as resolver:
        _present_macos_recovery_quotes([parent])
        resolver.assert_not_called()
    quote = child.messaging["quoted_reply"]
    assert quote["resource_basis"] == "authenticated_macos_forwarded_inline_quoted_app_card_v1"
    assert quote["resource"]["label"] == "广岛之恋 <b>literal</b>"
    assert quote["resource"]["publisher_name"] == "QQ音乐"
    assert "resource_original" not in quote
    assert (parent.content_text, proof, child.messaging["provenance"]) == before
    parent.messaging["_macos_recovery_validated"] = True
    _present_macos_recovery_quotes([parent])
    assert "resource" not in quote


@pytest.mark.parametrize("app_card", [False, True])
def test_mac_nested_projection_never_clears_an_authenticated_ios_projection(app_card):
    fixture = _nested_fixture(app_card=app_card)
    quote = _run_nested(fixture)
    assert "resource" in quote
    previous = deepcopy(quote)
    _present_macos_recovery_quotes([fixture.parent])
    assert fixture.child.messaging["quoted_reply"] == previous


@pytest.mark.parametrize("kind", ["app", "file"])
def test_mac_app_quote_retains_exact_original_metadata_without_payload_reads(mac_page, kind):
    reply, original = _app_quote(mac_page, kind)
    before = deepcopy(
        (
            reply.content_text, reply.messaging["provenance"],
            original.content_text, original.messaging["provenance"],
        )
    )
    with (
        patch("app.core.wechat_mac_resources.WechatMacResourceResolver.resolve") as resolver,
        patch(
            "app.core.wechat_history_browser.present_resource", wraps=present_resource
        ) as project,
    ):
        _present_macos_recovery_quotes([reply], originals=[original])
        resolver.assert_not_called()
        retained = project.call_args.args[0]
        assert retained["safe_title"] == "A literal <b>card</b> title"
        assert retained["publisher_name"] == "Synthetic publisher"
        assert retained["app_subtype"] == ("4" if kind == "app" else "6")
    quote = reply.messaging["quoted_reply"]
    assert quote["resource_basis"] == "exact_authenticated_macos_resource_original_v1"
    assert quote["resource_original"]["message_id"] == original.stable_id
    assert quote["resource"]["kind"] == kind
    assert quote["resource"]["label"] == "A literal <b>card</b> title"
    assert quote["resource"]["status"] == "metadata_only"
    assert quote["resource"]["can_reveal"] is False
    assert quote["text"] == "Original quoted content is unavailable."
    assert (
        reply.content_text, reply.messaging["provenance"],
        original.content_text, original.messaging["provenance"],
    ) == before
    projected = deepcopy(quote)
    _present_macos_recovery_quotes([reply], originals=[original])
    assert quote == projected


@pytest.mark.parametrize(
    "change",
    [
        "type", "sender", "create_time", "reference_time", "source_id", "server_id",
        "resource_account", "resource_peer", "resource_kind", "native_time",
        "availability", "original_kind", "untrusted", "duplicate", "missing",
    ],
)
def test_mac_app_quote_rejects_conflicting_original_metadata(mac_page, change):
    reply, original = _app_quote(mac_page)
    proof = original.messaging["provenance"]
    receipt = original.messaging["_macos_recovery_receipt"]
    originals = [original]
    if change == "type":
        proof["message_type"] = 3
    elif change == "sender":
        original.messaging["sender_id"] = OWNER
    elif change in {"source_id", "server_id", "create_time"}:
        receipt[change] = receipt[change] + 1 if change == "create_time" else "different"
        proof["macos_recovery"][change] = receipt[change]
    elif change == "reference_time":
        for reference in (
            reply.messaging["provenance"]["quoted_reference"],
            reply.messaging["quoted_reply"]["reference"],
        ):
            reference["source_timestamp"] = "1700000009"
    elif change in {
        "resource_account", "resource_peer", "resource_kind", "native_time", "availability"
    }:
        field = {
            "resource_account": "account_id",
            "resource_peer": "peer_id",
            "resource_kind": "kind",
            "native_time": "native_time",
            "availability": "availability",
        }[change]
        proof["resource_reference"][field] = "different"
    elif change == "original_kind":
        proof["text_extraction_kind"] = "quoted_reply"
    elif change == "untrusted":
        original.messaging["_macos_recovery_validated"] = True
    elif change == "duplicate":
        duplicate = deepcopy(original)
        duplicate.messaging["_macos_recovery_validated"] = _MACOS_RECOVERY_PRESENTATION
        originals.append(duplicate)
    else:
        originals = []
    _present_macos_recovery_quotes([reply], originals=originals)
    quote = reply.messaging["quoted_reply"]
    assert quote["resource_basis"] == "authenticated_macos_nontext_quote_type_v1"
    assert quote["resource"]["kind"] == "app"
    assert quote["resource"]["label"] == "App message"
    assert "resource_original" not in quote


def test_mac_app_quote_renders_literal_card_title(mac_page):
    reply, original = _app_quote(mac_page)
    _present_macos_recovery_quotes([reply], originals=[original])
    environment = Environment(
        loader=FileSystemLoader("app/web/templates"), autoescape=select_autoescape(["html"])
    )
    environment.filters["wechat_source_clock"] = format_wechat_source_clock
    module = environment.get_template("_wechat_browser.html").make_module(
        {"text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=[])}
    )
    soup = BeautifulSoup(str(module.message_row(reply)), "html.parser")
    resource = soup.select_one("blockquote .wechat-resource")
    assert resource["data-wechat-resource-kind"] == "app"
    assert "A literal <b>card</b> title" in resource.get_text()
    assert resource.find("b") is None
    assert not resource.find_all(["a", "img", "video", "audio", "button", "script"])
    assert "Original quoted content is unavailable." not in soup.get_text()


def _group_quote(mac_page):
    room = "98000000002@chatroom"
    session = "wechat:" + "e" * 64
    context = deepcopy(mac_page.reply.messaging)
    context.update(kind="group", peer_id=room)
    context["_macos_recovery_validated"] = _MACOS_RECOVERY_PRESENTATION
    proof = context["provenance"]
    for receipt in (context["_macos_recovery_receipt"], proof["macos_recovery"]):
        receipt.update(peer_id=room, session_id=session)
    parsed = extract_wechat_text(
        49,
        (
            "<msg><appmsg><type>57</type><title>Current reply</title><refermsg>"
            f"<type>49</type><fromusr>{room}</fromusr><chatusr>{PEER}</chatusr>"
            "<displayname></displayname><svrid>210</svrid>"
            "<createtime>1700000010</createtime><content><msg><appmsg>"
            "<type>57</type><title>Earlier reply</title><refermsg><type>1</type>"
            "<content>A deeper quoted message</content></refermsg>"
            "</appmsg></msg></content></refermsg></appmsg></msg>"
        ),
    )
    proof.update(
        {key: value for key, value in parsed.items() if key not in {"text", "flags"}}
    )
    context.update(_native_quoted_reply_presentation(proof))
    return replace(
        mac_page.reply, conversation_id=session, content_text=parsed["text"], messaging=context
    )


def test_mac_group_quote_resolves_empty_label_through_its_native_author(mac_page):
    item = _group_quote(mac_page)
    context = item.messaging
    quote = context["quoted_reply"]
    before = deepcopy((item.content_text, context["provenance"], quote))
    repository = WechatContactRepository(mac_page.root)
    names = SimpleNamespace(contact_name=lambda contact: "789")
    with patch.object(repository, "resolve_messages", wraps=repository.resolve_messages) as resolve:
        _resolve_macos_recovery_quoted_contacts([item], repository, names)
        request = resolve.call_args.args[0][0]
    assert request["conversation_kind"] == "group"
    assert request["peer_id"] == "98000000002@chatroom"
    assert request["sender_id"] == PEER
    assert request["sender_label"] == ""
    assert quote["viewer_display_name"] == "789"
    assert quote["contact"]["identity_status"] == "verified"
    assert quote["contact"]["id"] == canonical_contact_id(
        PEER, authority="native_message_sender"
    )
    assert quote["speaker"] == "Quoted message"
    assert quote["reference"]["source_sender_label"] == ""
    assert quote["text"] == "Earlier reply"
    assert "A deeper quoted message" not in quote["text"]
    source_quote = {
        key: value
        for key, value in quote.items()
        if key not in {"viewer_display_name", "contact"}
    }
    assert (item.content_text, context["provenance"], source_quote) == before
    projected = deepcopy(quote)
    _resolve_macos_recovery_quoted_contacts([item], repository, names)
    assert quote == projected


@pytest.mark.parametrize(
    "case",
    [
        "verified", "viewer_preference", "ambiguous_remark", "other_owner_only",
        "wrong_room", "wrong_partition", "reply_sender", "unregistered",
        "untrusted", "export_binding", "wrong_snapshot", "numeric_identifier",
    ],
)
def test_mac_literal_group_quote_resolves_only_an_exact_scoped_native_contact(mac_page, case):
    item = _group_quote(mac_page)
    context = item.messaging
    room = context["peer_id"]
    source = context["partition_id"]
    sender = "2987654" if case == "numeric_identifier" else "legacy_author_47"
    from_user = "98000000003@chatroom" if case == "wrong_room" else room
    parsed = extract_wechat_text(
        49,
        "<msg><appmsg><type>57</type><title>Literal reply</title><refermsg>"
        f"<type>3</type><fromusr>{from_user}</fromusr><chatusr>{sender}</chatusr>"
        "<displayname>Source nickname</displayname><svrid>210</svrid>"
        "<createtime>1700000010</createtime></refermsg></appmsg></msg>",
    )
    proof = context["provenance"]
    proof.update({key: value for key, value in parsed.items() if key not in {"text", "flags"}})
    context.update(_native_quoted_reply_presentation(proof))
    item = replace(item, content_text=parsed["text"])
    if case == "wrong_partition":
        context["partition_id"] = "ios-other-owner"
    if case == "reply_sender":
        context["sender_id"] = sender
    if case == "untrusted":
        context["_macos_recovery_validated"] = True
    if case != "unregistered":
        with sqlite3.connect(contact_registry_path(mac_page.root)) as db:
            contact_id = insert_contact(db, sender)
            bind(
                db, contact_id, source=source, sender=sender,
                snapshot="different-snapshot" if case == "wrong_snapshot" else "",
                authority="corroborated_export_username" if case == "export_binding" else "native_contact_username",
            )
            observe(db, contact_id, "remark", "Different owner's remark", source=source, owner="wxid_other_owner")
            observe(db, contact_id, "remark", "Different room's remark", source=source, owner=OWNER, room="98000000003@chatroom")
            if case != "other_owner_only":
                for snapshot in ("prior-capture-a", "prior-capture-b"):
                    observe(db, contact_id, "remark", "Canonical remark", source=source, owner=OWNER, snapshot=snapshot)
            if case == "ambiguous_remark":
                observe(db, contact_id, "remark", "Another retained remark", source=source, owner=OWNER)
    quote = context["quoted_reply"]
    before = deepcopy((item.content_text, proof, quote["reference"], quote["speaker"]))
    repository = WechatContactRepository(mac_page.root)
    names = SimpleNamespace(contact_name=lambda contact: "Viewer preference" if case == "viewer_preference" else "")
    _resolve_macos_recovery_quoted_contacts([item], repository, names)
    expected = {
        "verified": ("Canonical remark", "same_account_contact_remark"),
        "viewer_preference": ("Viewer preference", "local_user_preference"),
        "ambiguous_remark": ("Source nickname", "canonical_contact"),
        "other_owner_only": ("Source nickname", "canonical_contact"),
    }.get(case)
    if expected is None:
        assert "viewer_display_name" not in quote
        assert "contact" not in quote
    else:
        assert quote["viewer_display_name"] == expected[0]
        assert quote["contact"]["viewer_display_name_source"] == expected[1]
        assert quote["contact"]["id"] == contact_id
    assert (item.content_text, proof, quote["reference"], quote["speaker"]) == before


@pytest.mark.parametrize(
    "change",
    ["wrong_room", "invalid_user", "unknown_contact", "untrusted", "wrong_kind", "peer_mismatch"],
)
def test_mac_group_quote_rejects_unverified_or_cross_room_authors(mac_page, change):
    item = _group_quote(mac_page)
    context = item.messaging
    quote = context["quoted_reply"]
    if change in {"wrong_room", "invalid_user", "unknown_contact"}:
        field, value = {
            "wrong_room": ("source_sender_id", "98000000003@chatroom"),
            "invalid_user": ("source_chat_id", "Unknown source label"),
            "unknown_contact": ("source_chat_id", "wxid_unknown_group_author"),
        }[change]
        for reference in (
            context["provenance"]["quoted_reference"], quote["reference"],
            context["provenance"]["text_components"][1], quote["text_components"][1],
        ):
            reference[field] = value
    elif change == "untrusted":
        context["_macos_recovery_validated"] = True
    elif change == "wrong_kind":
        context["kind"] = "direct"
    else:
        context["peer_id"] = "98000000003@chatroom"
    before = deepcopy((item.content_text, context["provenance"], quote))
    _resolve_macos_recovery_quoted_contacts(
        [item], WechatContactRepository(mac_page.root),
        SimpleNamespace(contact_name=lambda contact: "789"),
    )
    assert "viewer_display_name" not in quote
    assert "contact" not in quote
    assert (item.content_text, context["provenance"], quote) == before


def _mac_group_scope(item, room):
    context = item.messaging
    session = "wechat:" + hashlib.sha256(room.encode()).hexdigest()
    context.update(kind="group", peer_id=room)
    context["_macos_recovery_validated"] = _MACOS_RECOVERY_PRESENTATION
    for receipt in (context["_macos_recovery_receipt"], context["provenance"]["macos_recovery"]):
        receipt.update(
            peer_id=room, session_id=session,
            table="Msg_" + hashlib.md5(room.encode(), usedforsecurity=False).hexdigest(),
        )
    return replace(item, conversation_id=session)


def test_mac_short_video_quote_retains_a_typed_placeholder_without_payload_reads(mac_page):
    item = mac_page.reply
    for reference in (
        item.messaging["provenance"]["quoted_reference"],
        item.messaging["quoted_reply"]["reference"],
    ):
        reference["original_message_type"] = 62
    quote = item.messaging["quoted_reply"]
    for field in ("resource", "resource_basis", "resource_original"):
        quote.pop(field, None)
    before = deepcopy((item.content_text, item.messaging["provenance"]))
    with patch("app.core.wechat_mac_resources.WechatMacResourceResolver.resolve") as resolver:
        _present_macos_recovery_quotes([item])
        resolver.assert_not_called()
    assert quote["resource"]["kind"] == "video"
    assert quote["resource"]["status"] == "metadata_only"
    assert quote["resource"]["can_reveal"] is False
    assert "resource_original" not in quote
    assert (item.content_text, item.messaging["provenance"]) == before


@pytest.mark.parametrize("wrong_room", [False, True])
def test_verified_mac_im_group_quote_uses_only_its_exact_room(mac_page, wrong_room):
    item = _mac_group_scope(_group_quote(mac_page), "99000000001@im.chatroom")
    context = item.messaging
    old_room = "98000000002@chatroom"
    room = "99000000002@im.chatroom" if wrong_room else context["peer_id"]
    for reference in (
        context["provenance"]["quoted_reference"],
        context["quoted_reply"]["reference"],
        context["provenance"]["text_components"][1],
        context["quoted_reply"]["text_components"][1],
    ):
        if reference.get("source_sender_id") == old_room:
            reference["source_sender_id"] = room
    before = deepcopy((item.content_text, context["provenance"]))
    quote = context["quoted_reply"]
    _resolve_macos_recovery_quoted_contacts(
        [item], WechatContactRepository(mac_page.root),
        SimpleNamespace(contact_name=lambda contact: "Verified group speaker"),
    )
    if wrong_room:
        assert "viewer_display_name" not in quote
        assert "contact" not in quote
    else:
        assert quote["viewer_display_name"] == "Verified group speaker"
        assert quote["contact"]["id"] == canonical_contact_id(
            PEER, authority="native_message_sender"
        )
    assert (item.content_text, context["provenance"]) == before


def _legacy_mac_group_quote(mac_page):
    legacy = "legacy_native_username"
    room = "98000000002@chatroom"
    reply, original = _app_quote(mac_page)
    reply, original = (_mac_group_scope(item, room) for item in (reply, original))
    original.messaging["sender_id"] = legacy
    reference = reply.messaging["provenance"]["quoted_reference"]
    reference.update(
        source_sender_id=legacy, source_sender_label="", source_chat_id=room,
        source_timestamp=str(original.messaging["_macos_recovery_receipt"]["create_time"]),
    )
    reply.messaging.update(_native_quoted_reply_presentation(reply.messaging["provenance"]))
    path = contact_registry_path(mac_page.root)
    with sqlite3.connect(path) as connection:
        identifier = insert_contact(connection, legacy)
        bind(
            connection, identifier, source=reply.messaging["partition_id"], sender=legacy,
            snapshot=reply.messaging["_macos_recovery_receipt"]["snapshot_id"], room=room,
        )
        observe(
            connection, identifier, "nickname", "Verified legacy speaker",
            source=reply.messaging["partition_id"],
            snapshot=reply.messaging["_macos_recovery_receipt"]["snapshot_id"], owner=OWNER,
        )
    return reply, original, identifier


@pytest.mark.parametrize("room_valued", [False, True])
def test_mac_quote_legacy_author_requires_exact_original_and_verified_contact(mac_page, room_valued):
    reply, original, identifier = _legacy_mac_group_quote(mac_page)
    if room_valued:
        reply.messaging["provenance"]["quoted_reference"].update(
            source_sender_id=reply.messaging["peer_id"],
            source_chat_id=reply.messaging["sender_id"],
        )
        reply.messaging.update(_native_quoted_reply_presentation(reply.messaging["provenance"]))
    before = deepcopy(
        (reply.content_text, reply.messaging["provenance"], original.messaging),
        {id(_MACOS_RECOVERY_PRESENTATION): _MACOS_RECOVERY_PRESENTATION},
    )
    registry = contact_registry_path(mac_page.root).read_bytes()
    _resolve_macos_recovery_quoted_contacts(
        [reply], WechatContactRepository(mac_page.root),
        SimpleNamespace(contact_name=lambda contact: ""), originals=[original],
    )
    quote = reply.messaging["quoted_reply"]
    assert quote["contact"]["id"] == identifier
    assert quote["viewer_display_name"] == "Verified legacy speaker"
    assert quote["speaker"] == "Quoted message"
    assert (reply.content_text, reply.messaging["provenance"], original.messaging) == before
    assert contact_registry_path(mac_page.root).read_bytes() == registry


@pytest.mark.parametrize(
    "change",
    [
        "source_id", "snapshot_id", "account_id", "peer_id", "main_sha256",
        "wal_sha256", "database_relative_path", "table", "sender", "local_id",
        "create_time", "reference_time", "missing_reference_time", "type", "duplicate",
        "untrusted", "missing",
    ],
)
def test_mac_legacy_quote_author_never_bypasses_exact_original_guards(mac_page, change):
    reply, original, _identifier = _legacy_mac_group_quote(mac_page)
    context = original.messaging
    originals = [original]
    if change in {
        "source_id", "snapshot_id", "account_id", "peer_id", "main_sha256",
        "wal_sha256", "database_relative_path", "table",
    }:
        for receipt in (context["_macos_recovery_receipt"], context["provenance"]["macos_recovery"]):
            receipt[change] = "different"
    elif change == "sender":
        context["sender_id"] = "another_legacy_username"
    elif change in {"local_id", "create_time"}:
        for receipt in (context["_macos_recovery_receipt"], context["provenance"]["macos_recovery"]):
            receipt[change] = reply.messaging["_macos_recovery_receipt"][change] + 1
    elif change in {"reference_time", "missing_reference_time"}:
        reply.messaging["provenance"]["quoted_reference"]["source_timestamp"] = (
            "1700009999" if change == "reference_time" else ""
        )
        reply.messaging.update(_native_quoted_reply_presentation(reply.messaging["provenance"]))
    elif change == "type":
        context["provenance"]["message_type"] = 3
    elif change == "duplicate":
        duplicate = deepcopy(original)
        duplicate.messaging["_macos_recovery_validated"] = _MACOS_RECOVERY_PRESENTATION
        originals.append(duplicate)
    elif change == "untrusted":
        context["_macos_recovery_validated"] = True
    else:
        originals.clear()
    before = deepcopy((reply.content_text, reply.messaging["provenance"], reply.messaging["quoted_reply"]))
    _resolve_macos_recovery_quoted_contacts(
        [reply], WechatContactRepository(mac_page.root),
        SimpleNamespace(contact_name=lambda contact: "Verified legacy speaker"), originals=originals,
    )
    quote = reply.messaging["quoted_reply"]
    assert "viewer_display_name" not in quote and "contact" not in quote
    assert (reply.content_text, reply.messaging["provenance"], quote) == before
