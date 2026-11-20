"""Resource presentation and Finder routes retain account and host boundaries.

Code version: v1.5.1-codex.0
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core.config import CrawlConfig
from app.core.chat_history_browser import query_chat_history
from app.core.wechat_archive import import_wechat_archive
from app.core.wechat_resources import (
    present_resource,
    resolve_resource_for_reveal,
    resource_reference,
)
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests.test_wechat_archive import make_archive
from tests.test_wechat_browser_read import _done
from tests.wechat_browser_markup import get_verified_browser_page, parse_browser_markup


OWNER = "wxid_fixture_owner"
PEER = "fixture_peer@chatroom"
REFERENCE = {"kind": "image", "account_id": OWNER, "peer_id": PEER}
_METADATA_CARDS = (
    ("image", "Image"), ("sticker", "Sticker"), ("video", "Video"),
    ("audio", "Voice message"), ("file", "File"), ("app", "App message"),
    ("unknown", "Message attachment"), ("contact", "Contact card"), ("location", "Location"),
)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"resource_reference": {**REFERENCE, "account_id": "wxid_other_owner"}},
        {"resource_reference": {**REFERENCE, "peer_id": "another_room@chatroom"}},
        {"resource_reference": {**REFERENCE, "kind": "executable"}},
        {"resource_reference": {**REFERENCE, "kind": []}},
        {"resource_reference": {**REFERENCE, "kind": {}}},
    ],
)
def test_unverified_resource_scope_is_not_linked_to_a_mac_file(value):
    assert resource_reference(value, OWNER, PEER) is None


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("file", "Available locally"),
        ("candidate", "Local file candidate"),
        ("folder", "Local folder only"),
        ("missing", "Not found locally"),
    ],
)
def test_public_presentation_reports_local_availability_without_host_path_leaks(status, label):
    private_path = Path("/private/synthetic-wechat-account/resource")
    resolver = Mock()
    resolver.resolve.return_value = {
        "status": status,
        "path": private_path if status != "missing" else None,
        "label": "Show the conversation image folder in Finder",
        "evidence": "private_backend_locator",
    }
    item = present_resource(REFERENCE, resolver)
    assert item["label"] == "Image"
    assert item["kind"] == "image"
    assert item["status"] == status
    assert item["can_reveal"] == (status != "missing")
    assert str(private_path) not in json.dumps(item)
    assert "path" not in item and "evidence" not in item
    assert item["status_label"] == label


@pytest.mark.parametrize(
    ("publisher", "app_name", "expected"),
    [
        ("ShanghaiBUYBUY", "WeChat", "ShanghaiBUYBUY"),
        ("政事儿", "WeChat", "政事儿"),
        ("", "知乎", "知乎"),
        (None, "铁路12306", "铁路12306"),
        ("bad\x00source", "", ""),
        ("x" * 1025, "", ""),
        ([], {}, ""),
    ],
)
def test_app_card_keeps_explicit_publisher_separate_from_title_and_sender(
    publisher, app_name, expected
):
    reference = {
        "kind": "app",
        "availability": "metadata_only",
        "app_subtype": "5",
        "safe_title": "A source article title",
        "publisher_name": publisher,
        "app_name": app_name,
    }
    before = json.dumps(reference, ensure_ascii=False)
    resolver = Mock()
    result = present_resource(reference, resolver)
    assert result["label"] == "A source article title"
    assert result["publisher_name"] == expected
    assert result["status"] == "metadata_only"
    assert result["can_reveal"] is False
    resolver.resolve.assert_not_called()
    assert json.dumps(reference, ensure_ascii=False) == before


def test_forwarded_conversation_does_not_acquire_an_article_publisher():
    resource = present_resource(
        {
            "kind": "app",
            "availability": "metadata_only",
            "app_subtype": "19",
            "safe_title": "Participants and excerpts",
            "publisher_name": "A forwarding application",
        },
        None,
    )
    assert resource["label"] == "Chat history"
    assert resource["publisher_name"] == ""


@pytest.mark.parametrize(
    ("kind", "subtype", "availability", "expected"),
    [
        ("app", "5", "metadata_only", True),
        ("app", "19", "metadata_only", False),
        ("app", "33", "metadata_only", False),
        ("app", None, "metadata_only", False),
        ("app", 5, "metadata_only", False),
        ("app", "5", "file", False),
        ("file", "5", "metadata_only", False),
        ("video", "5", "metadata_only", False),
        ("audio", "5", "metadata_only", False),
    ],
)
def test_only_explicit_metadata_app_link_subtypes_use_link_presentation(
    kind, subtype, availability, expected
):
    resolver = Mock()
    resolver.resolve.return_value = {
        "status": "file",
        "path": Path("/private/synthetic-resource"),
        "label": "Show in Finder",
    }
    resource = present_resource(
        {"kind": kind, "availability": availability, "app_subtype": subtype},
        resolver,
    )
    assert resource.get("is_link", False) is expected
    assert resource["status"] == availability
    assert resource["can_reveal"] is (availability == "file")


def test_app_card_publisher_is_rendered_as_inert_text():
    from bs4 import BeautifulSoup
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    environment = Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=select_autoescape(["html"]),
    )
    module = environment.get_template("_wechat_browser.html").make_module(
        {"text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=[])}
    )
    source = '<script>alert("source")</script>'
    resource = present_resource(
        {
            "kind": "app",
            "availability": "metadata_only",
            "app_subtype": "5",
            "safe_title": "Article",
            "publisher_name": source,
        },
        None,
    )
    soup = BeautifulSoup(str(module.resource_card(resource)), "html.parser")
    assert soup.select_one(".wechat-resource-label").get_text() == "Article"
    assert soup.select_one(".wechat-resource-publisher").get_text() == source
    assert soup.select_one(".wechat-resource-status").get_text() == source
    assert soup.select_one(".wechat-resource")["data-wechat-resource-link"] == "true"
    assert "Metadata only" not in soup.get_text()
    assert not soup.find_all(["script", "a", "img", "iframe", "button"])


@pytest.mark.parametrize(
    ("publisher", "app_name", "expected"),
    [
        ("Source publisher", "WeChat", "Source publisher"),
        ("", "Source app", "Source app"),
        ("", "", ""),
    ],
)
@pytest.mark.parametrize("embedded", [False, True])
def test_link_cards_keep_only_available_publisher_without_a_separator(
    publisher, app_name, expected, embedded
):
    from bs4 import BeautifulSoup
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    environment = Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=select_autoescape(["html"]),
    )
    module = environment.get_template("_wechat_browser.html").make_module(
        {"text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=[])}
    )
    resource = present_resource(
        {
            "kind": "app",
            "availability": "metadata_only",
            "app_subtype": "5",
            "safe_title": "Forwarded article",
            "publisher_name": publisher,
            "app_name": app_name,
        },
        None,
    )
    soup = BeautifulSoup(
        str(module.resource_card(resource, embedded=embedded)), "html.parser"
    )
    card = soup.select_one(".wechat-resource")
    assert card["data-wechat-resource-kind"] == "app"
    assert card["data-wechat-resource-status"] == "metadata_only"
    assert card["data-wechat-resource-link"] == "true"
    assert "Metadata only" not in card.get_text()
    assert "·" not in card.get_text()
    status = card.select_one(".wechat-resource-status")
    assert (status.get_text() if status else "") == expected
    assert bool(status) is bool(expected)
    assert not card.has_attr("data-wechat-resource-reveal")


@pytest.mark.parametrize("placement", ["quoted", "forwarded"])
def test_quoted_and_forwarded_link_cards_reuse_publisher_only_presentation(placement):
    from bs4 import BeautifulSoup

    from tests.test_wechat_html_forwarded import _macro_message, _message_macro_module

    resource = present_resource(
        {
            "kind": "app",
            "availability": "metadata_only",
            "app_subtype": "5",
            "safe_title": "Nested article",
            "publisher_name": "Original publisher",
        },
        None,
    )
    if placement == "quoted":
        message = _macro_message(text="A reply to the article")
        message.messaging["quoted_reply"] = {
            "speaker": "Original sender",
            "resource": resource,
        }
        selector = ".wechat-message-quote .wechat-resource"
    else:
        child = _macro_message()
        child.stable_id = "synthetic-nested-article"
        child.messaging["resource"] = resource
        message = _macro_message({"title": "Chat history", "items": [child]})
        selector = "[data-wechat-forwarded] .wechat-resource"
    rendered = str(_message_macro_module().message_row(message))
    card = BeautifulSoup(rendered, "html.parser").select_one(selector)
    assert card is not None
    assert card["data-wechat-resource-link"] == "true"
    assert card.select_one(".wechat-resource-label").get_text() == "Nested article"
    assert card.select_one(".wechat-resource-status").get_text() == "Original publisher"
    assert "Metadata only" not in card.get_text()
    assert "·" not in card.get_text()
    assert not card.has_attr("data-wechat-resource-reveal")


@pytest.mark.parametrize("publisher", ["Original publisher", ""])
@pytest.mark.parametrize("embedded", [False, True])
def test_non_link_app_metadata_cards_keep_only_the_available_publisher(publisher, embedded):
    from bs4 import BeautifulSoup
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    environment = Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=select_autoescape(["html"]),
    )
    module = environment.get_template("_wechat_browser.html").make_module(
        {"text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=[])}
    )
    resource = present_resource(
        {
            "kind": "app", "availability": "metadata_only", "app_subtype": "4",
            "safe_title": "Original card title", "publisher_name": publisher,
        },
        None,
    )
    soup = BeautifulSoup(
        str(module.resource_card(resource, embedded=embedded)), "html.parser"
    )
    card = soup.select_one(".wechat-resource")
    assert not card.has_attr("data-wechat-resource-link")
    assert card["data-wechat-resource-status"] == "metadata_only"
    assert card.select_one(".wechat-resource-label").get_text() == "Original card title"
    status = card.select_one(".wechat-resource-status")
    assert (status.get_text() if status else "") == publisher
    assert bool(status) is bool(publisher)
    assert "Metadata only" not in card.get_text()
    assert "·" not in card.get_text()
    assert not card.select_one("[data-wechat-resource-reveal]")


@pytest.mark.parametrize("embedded", [False, True])
@pytest.mark.parametrize("kind", ["image", "sticker", "file"])
@pytest.mark.parametrize("status", ["file", "candidate", "folder", "missing"])
def test_resolved_resource_keeps_its_status_and_separate_finder_action(embedded, kind, status):
    from bs4 import BeautifulSoup
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    environment = Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=select_autoescape(["html"]),
    )
    module = environment.get_template("_wechat_browser.html").make_module(
        {
            "text_page": SimpleNamespace(
                messaging={}, session_detail=False, sessions=[]
            ),
            "url_for": lambda *_args, **_kwargs: "/synthetic-resource/reveal",
        }
    )
    resolver = Mock()
    resolver.resolve.return_value = {
        "status": status,
        "path": None if status == "missing" else Path("/private/synthetic-resource"),
        "label": "Show in Finder",
    }
    resource = present_resource({**REFERENCE, "kind": kind}, resolver)
    rendered = str(module.resource_card(resource, "synthetic-image", embedded))
    card = BeautifulSoup(rendered, "html.parser").select_one(".wechat-resource")
    assert card.name == "div"
    assert card["data-wechat-resource-status"] == status
    assert not card.has_attr("data-wechat-resource-reveal")
    feedback = card.select_one("[data-wechat-resource-feedback]")
    assert feedback.get_text() == resource["status_label"]
    button = card.select_one("button[data-wechat-resource-reveal]")
    if embedded or status == "missing":
        assert button is None
    else:
        assert "circular-icon-button" in button["class"]
        assert button["type"] == "button"
        assert button["aria-label"] == resource["label"] + ": Show in Finder"
        assert button.select_one(".icon.browser-media-reveal-icon") is not None
        assert button["aria-describedby"] == feedback["id"]


@pytest.mark.parametrize(
    ("kind", "expected"), [("group", "Group chat"), ("direct", "Private chat")]
)
def test_selected_conversation_header_contains_only_its_standard_kind_badge(
    kind, expected
):
    from bs4 import BeautifulSoup
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    environment = Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=select_autoescape(["html"]),
    )
    origin = {
        "kind": kind,
        "viewer_account_label": "Synthetic account",
        "account_label": "Original account",
        "identity_status": "confirmed",
        "source_label": "Synthetic source",
        "mac_account_mapping": {"status": "mapped"},
    }
    before = json.dumps(origin)
    rendered = environment.get_template("_wechat_browser.html").render(
        text_page=SimpleNamespace(
            messaging={},
            session_detail=True,
            current_session=SimpleNamespace(messaging=origin),
            items=[],
        )
    )
    header = BeautifulSoup(rendered, "html.parser").select_one(
        ".wechat-conversation-labels"
    )
    assert header.get_text(strip=True) == expected
    assert len(header.select(":scope > *")) == 1
    assert header.select_one(".wechat-kind-badge").get_text() == expected
    assert json.dumps(origin) == before


@pytest.mark.parametrize(("kind", "label"), _METADATA_CARDS)
@pytest.mark.parametrize("embedded", [False, True])
def test_metadata_only_resource_cards_omit_visible_availability(kind, label, embedded):
    from bs4 import BeautifulSoup
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    environment = Environment(
        loader=FileSystemLoader("app/web/templates"), autoescape=select_autoescape(["html"])
    )
    module = environment.get_template("_wechat_browser.html").make_module(
        {"text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=[])}
    )
    resource = {
        "kind": kind, "kind_label": label, "label": label, "status": "metadata_only",
        "status_label": "Metadata only", "action_label": "", "can_reveal": False,
    }
    before = json.dumps(resource, sort_keys=True)
    soup = BeautifulSoup(
        str(module.resource_card(resource, embedded=embedded)), "html.parser"
    )
    card = soup.select_one(".wechat-resource")
    assert card["data-wechat-resource-status"] == "metadata_only"
    assert soup.select_one(".wechat-resource-label").get_text() == resource["label"]
    assert soup.select_one(".wechat-resource-status") is None
    assert "Metadata only" not in card.get_text()
    assert not card.select_one("[data-wechat-resource-reveal]")
    assert not card.select_one("[data-wechat-resource-feedback]")
    assert card.select_one(".wechat-resource-icon")["aria-hidden"] == "true"
    assert resource["status"] == "metadata_only"
    assert resource["can_reveal"] is False
    assert json.dumps(resource, sort_keys=True) == before


def test_forwarded_metadata_resources_share_the_status_free_inert_cards():
    from bs4 import BeautifulSoup

    from tests.test_wechat_html_forwarded import _macro_message, _message_macro_module

    children = []
    for kind, label in _METADATA_CARDS:
        child = _macro_message(text="")
        child.stable_id = "synthetic-forwarded-" + kind
        child.messaging["resource"] = {
            "kind": kind, "kind_label": label, "label": label, "status": "metadata_only",
            "status_label": "Metadata only", "action_label": "", "can_reveal": False,
        }
        children.append(child)
    parent = _macro_message({"title": "Chat history", "items": children})
    rendered = str(_message_macro_module().message_row(parent))
    soup = BeautifulSoup(rendered, "html.parser")
    cards = soup.select("[data-wechat-forwarded] .wechat-resource")
    assert [card["data-wechat-resource-kind"] for card in cards] == [
        kind for kind, _label in _METADATA_CARDS
    ]
    assert [card.select_one(".wechat-resource-label").get_text() for card in cards] == [
        label for _kind, label in _METADATA_CARDS
    ]
    for card in cards:
        assert card["data-wechat-resource-status"] == "metadata_only"
        assert card.select_one(".wechat-resource-status") is None
        assert "Metadata only" not in card.get_text()
        assert not card.find_all(["a", "img", "video", "audio", "button", "script"])


def test_finder_resolution_requires_a_current_stored_reference_and_rechecks_the_mac(
    tmp_path,
):
    record = {
        "account_id": OWNER,
        "peer_id": PEER,
        "provenance": json.dumps({"resource_reference": REFERENCE}),
    }
    repository = SimpleNamespace(resource_message=Mock(return_value=record))
    destination = {"status": "folder", "path": tmp_path, "label": "Finder"}
    with (
        patch(
            "app.core.wechat_resources.WechatArchiveRepository", return_value=repository
        ),
        patch("app.core.wechat_resources.WechatMacResourceResolver") as resolver,
    ):
        resolver.return_value.resolve.return_value = destination
        assert resolve_resource_for_reveal(tmp_path, "fixture-resource") == destination
        repository.resource_message.assert_called_once_with("fixture-resource")
        resolver.return_value.resolve.assert_called_once_with(REFERENCE)
        resolver.return_value.resolve.return_value = {"status": "missing", "path": None}
        with pytest.raises(LookupError, match="no longer available"):
            resolve_resource_for_reveal(tmp_path, "fixture-resource")
        repository.resource_message.return_value = None
        with pytest.raises(LookupError, match="needs refreshing"):
            resolve_resource_for_reveal(tmp_path, "fixture-resource")


@pytest.fixture
def resource_app(tmp_path):
    store = tmp_path / "store"
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            store,
            computer_use_settings_path=tmp_path / "agent.json",
            computer_use_runtime_root=tmp_path / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True, TEST_STORE_ROOT=store)
    return app


def test_finder_route_uses_the_stored_destination_not_client_paths(
    resource_app, tmp_path
):
    destination = {"status": "folder", "path": tmp_path / "trusted-account"}
    with (
        patch(
            "app.web.local_resource_routes.resolve_resource_for_reveal",
            return_value=destination,
        ) as resolve,
        patch("app.web.local_resource_routes.reveal_media_path") as reveal,
        patch("app.web.local_resource_routes.open_directory_path") as open_folder,
    ):
        response = resource_app.test_client().post(
            "/api/browser/wechat/resources/fixture-reference/reveal",
            json={"path": "/etc/private-unrelated-file", "account_id": "unrelated"},
        )
        assert response.status_code == 200
        assert response.json["target_kind"] == "folder"
        resolve.assert_called_once_with(
            resource_app.config["TEST_STORE_ROOT"], "fixture-reference"
        )
        open_folder.assert_called_once_with(destination["path"])
        reveal.assert_not_called()
        assert str(tmp_path) not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "method,headers,environ",
    [
        ("GET", {}, {}),
        ("POST", {"Origin": "https://unrelated.example"}, {}),
        ("POST", {"Sec-Fetch-Site": "cross-site"}, {}),
        ("POST", {}, {"REMOTE_ADDR": "192.0.2.7"}),
    ],
)
def test_finder_route_cannot_be_prefetched_or_invoked_cross_site(
    resource_app, method, headers, environ
):
    with (
        patch("app.web.local_resource_routes.resolve_resource_for_reveal") as resolve,
        patch("app.web.local_resource_routes.reveal_media_path") as reveal,
    ):
        response = resource_app.test_client().open(
            "/api/browser/wechat/resources/fixture-reference/reveal",
            method=method,
            headers=headers,
            environ_overrides=environ,
        )
        assert response.status_code in {403, 405}
        resolve.assert_not_called()
        reveal.assert_not_called()


def test_missing_resource_returns_an_error_without_launching_finder(resource_app):
    with (
        patch(
            "app.web.local_resource_routes.resolve_resource_for_reveal",
            side_effect=LookupError("Not found on this Mac"),
        ),
        patch("app.web.local_resource_routes.reveal_media_path") as reveal,
    ):
        response = resource_app.test_client().post(
            "/api/browser/wechat/resources/fixture-reference/reveal", json={}
        )
        assert response.status_code == 404
        assert response.json["error"] == "Not found on this Mac"
        reveal.assert_not_called()


def test_native_finder_failure_does_not_expose_a_private_host_path(resource_app):
    private_path = Path("/private/synthetic-account/private-resource")
    with (
        patch(
            "app.web.local_resource_routes.resolve_resource_for_reveal",
            return_value={"status": "file", "path": private_path},
        ),
        patch(
            "app.web.local_resource_routes.reveal_media_path",
            side_effect=FileNotFoundError(2, "No such file", str(private_path)),
        ),
    ):
        response = resource_app.test_client().post(
            "/api/browser/wechat/resources/fixture-reference/reveal", json={}
        )
        assert response.status_code == 409
        assert str(private_path) not in response.get_data(as_text=True)
        assert "may have moved" in response.json["error"]


@pytest.mark.parametrize("status", ["file", "candidate"])
def test_exact_and_candidate_files_are_selected_without_opening_the_payload(
    resource_app, tmp_path, status
):
    destination = {"status": status, "path": tmp_path / "trusted-file"}
    with (
        patch(
            "app.web.local_resource_routes.resolve_resource_for_reveal",
            return_value=destination,
        ),
        patch("app.web.local_resource_routes.reveal_media_path") as reveal,
        patch("app.web.local_resource_routes.open_directory_path") as open_folder,
    ):
        response = resource_app.test_client().post(
            "/api/browser/wechat/resources/fixture-reference/reveal", json={}
        )
        assert response.status_code == 200
        reveal.assert_called_once_with(destination["path"])
        open_folder.assert_not_called()


@pytest.mark.parametrize("status", ["stale", "invalid"])
def test_unavailable_resource_index_is_explicit_on_the_conversation_index(
    resource_app, status
):
    page = query_chat_history(resource_app.config["TEST_STORE_ROOT"], source="wechat")
    page.messaging["resource_index_status"] = status
    client = resource_app.test_client()
    address = "/browser?view=text&source=wechat"
    with patch("app.web.local_resource_routes.query_chat_history", return_value=page):
        response = client.get(address)
        shell = parse_browser_markup(response.data).find(".//*[@data-wechat-read-pending]")
        if shell is not None and shell.get("data-state") == "running":
            service = resource_app.extensions["wechat_browser_read"]
            assert _done(service, shell.get("data-task-id"))["state"] == "failed"
            response = client.get(address)
    assert response.status_code == 200
    assert "Media and file references need refreshing" in response.get_data(
        as_text=True
    )


def test_an_old_account_without_resource_metadata_is_not_presented_as_complete(
    resource_app, tmp_path
):
    store = resource_app.config["TEST_STORE_ROOT"]
    import_wechat_archive(make_archive(tmp_path / "archive"), store)
    session = query_chat_history(
        store, source="wechat", account="account:wxid_account_a"
    ).sessions[0]
    page = query_chat_history(store, source="wechat", session=session.stable_id)
    page.messaging.update(resource_index_status="ready", resource_index_accounts=())
    with patch("app.web.local_resource_routes.query_chat_history", return_value=page):
        response = get_verified_browser_page(
            resource_app.test_client(),
            "/browser?view=text&source=wechat&session=" + session.stable_id,
        )
    assert response.status_code == 200
    assert (
        "Media and file references are unavailable in this source archive."
        in response.get_data(as_text=True)
    )
