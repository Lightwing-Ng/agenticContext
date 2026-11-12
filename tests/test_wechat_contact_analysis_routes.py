"""Contact navigation keeps return destinations local and source links precise.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

from flask import render_template
import pytest

from app.core.config import CrawlConfig
from app.core.wechat_contact_analysis import query_contact_analysis
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from app.web.local_resource_routes import _wechat_contact_return_url
from tests import test_wechat_contact_analysis as fixtures
from tests import test_wechat_forwarded_contact_analysis as forwarded_fixtures
from tests.wechat_browser_markup import get_verified_browser_page


CID = fixtures.CID
contact_analysis_store = fixtures.contact_analysis_store
forwarded_activity_store = forwarded_fixtures.forwarded_activity_store


@pytest.fixture
def contact_app(contact_analysis_store, tmp_path):
    store = contact_analysis_store.store
    query_contact_analysis(store, CID)
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
    app.config.update(TESTING=True)
    return app, contact_analysis_store


@pytest.mark.parametrize(
    "destination",
    [
        "https://outside.invalid/browser",
        "//outside.invalid/browser",
        "javascript:alert(1)",
        "/\\outside.invalid/browser",
        "/browser\r\nLocation: https://outside.invalid/",
        "/settings",
        "browser?view=text",
        "/browser?" + "q=" + "x" * 4096,
    ],
)
def test_contact_return_rejects_external_or_unrelated_destinations(
    contact_app, destination
):
    app, _ = contact_app
    with app.test_request_context("/browser"):
        result = _wechat_contact_return_url(destination)
    parsed = urlsplit(result)
    assert not parsed.netloc and not parsed.scheme and parsed.path == "/browser"
    assert parse_qs(parsed.query)["source"] == ["wechat"]
    assert result != destination


def test_return_keeps_the_exact_chat_account_page_search_and_anchor(contact_app):
    app, _ = contact_app
    destination = (
        "/browser?view=text&source=wechat&account=account%3Awxid_account_a"
        "&chat_kind=group&session=wechat%3Aroom&page=3&q=exact&sort=oldest"
        "#wechat_message_saved"
    )
    with app.test_request_context("/browser"):
        assert _wechat_contact_return_url(destination) == destination
    response = app.test_client().get(
        "/browser/wechat/contacts/" + CID,
        query_string={"return_to": destination},
    )
    assert response.status_code == 200
    assert 'target="_blank"' not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "identifier",
    [
        "not-canonical",
        "wechat:contact:source:unconfirmed",
        "wechat:contact:" + "0" * 64,
    ],
)
def test_unverified_or_missing_contact_cannot_open_an_aggregate(
    contact_app, identifier
):
    app, _ = contact_app
    assert (
        app.test_client()
        .get("/browser/wechat/contacts/" + quote(identifier, safe=""))
        .status_code
        == 404
    )


def test_busy_source_waits_without_reading_a_stale_cache(contact_app):
    app, fixture = contact_app
    journal = fixture.history.with_name(fixture.history.name + "-journal")
    journal.write_bytes(b"active source transaction")
    response = app.test_client().get("/browser/wechat/contacts/" + CID)
    assert response.status_code == 202
    assert "Refresh" not in response.headers
    assert "Gathering statements" in response.get_data(as_text=True)
    assert "data-contact-statement-id=" not in response.get_data(as_text=True)
    modal = (
        response.get_data(as_text=True)
        .split('id="cache_wait_modal"', 1)[1]
        .split('<div class="workspace-modal-dialog">', 1)[0]
    )
    assert "hidden" not in modal
    assert (
        '<main class="page wechat-contact-analysis-page" inert>'
        in response.get_data(as_text=True)
    )


def test_pending_poll_is_json_without_navigation_or_replacement_html(contact_app):
    app, fixture = contact_app
    fixture.history.with_name(fixture.history.name + "-journal").write_bytes(b"busy")
    response = app.test_client().get(
        "/browser/wechat/contacts/" + CID, headers={"Accept": "application/json"}
    )
    assert response.status_code == 202
    assert response.json["status"] == "indexing"
    assert response.json["contact_id"] == CID
    assert "html" not in response.json
    assert "Refresh" not in response.headers
    assert response.cache_control.no_store
    assert "Accept" in response.vary


def test_ready_poll_reuses_the_escaped_content_fragment(contact_app):
    app, _ = contact_app
    response = app.test_client().get(
        "/browser/wechat/contacts/" + CID, headers={"Accept": "application/json"}
    )
    assert response.status_code == 200
    assert response.json["status"] == "ready"
    assert response.json["contact_id"] == CID
    assert "data-contact-statement-id=" in response.json["html"]
    assert "<html" not in response.json["html"]
    assert "<script" not in response.json["html"]
    assert response.cache_control.no_store


def test_shared_waiting_modal_defaults_remain_closed_and_escape_optional_copy(
    contact_app,
):
    app, _ = contact_app
    with app.test_request_context("/browser"):
        default = render_template("_waiting_modal.html")
        opened = render_template(
            "_waiting_modal.html",
            wait_modal_initially_open=True,
            wait_modal_title="Loading <contact>",
            wait_modal_copy="Collecting <statements>",
        )
    assert "hidden" in default.split('<div class="workspace-modal-dialog">', 1)[0]
    assert "hidden" not in opened.split('<div class="workspace-modal-dialog">', 1)[0]
    assert "Working locally" in default
    assert "Loading &lt;contact&gt;" in opened
    assert "Collecting &lt;statements&gt;" in opened


def test_source_link_returns_to_the_original_account_and_message(contact_app):
    app, fixture = contact_app
    item = query_contact_analysis(fixture.store, CID)["items"][0]
    response = app.test_client().get("/browser/wechat/messages/" + item["id"])
    assert response.status_code == 302
    location = urlsplit(response.headers["Location"])
    query = parse_qs(location.query)
    assert location.path == "/browser"
    assert location.fragment == "wechat_message_" + item["id"]
    assert query["account"] == [item["account_key"]]
    assert query["session"] == [item["conversation_id"]]
    assert query["chat_kind"] == [item["kind"]]


def test_malformed_or_missing_message_source_is_not_redirected(contact_app):
    app, _ = contact_app
    for identifier in ("bad", "0" * 64):
        assert (
            app.test_client().get("/browser/wechat/messages/" + identifier).status_code
            == 404
        )


@pytest.fixture
def forwarded_contact_app(forwarded_activity_store, tmp_path):
    fixture = forwarded_activity_store
    query_contact_analysis(fixture["store"], forwarded_fixtures.CID)
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            fixture["store"],
            computer_use_settings_path=tmp_path / "forwarded-agent.json",
            computer_use_runtime_root=tmp_path / "forwarded-runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    return app, fixture


def test_forwarded_contact_row_links_to_its_original_account_and_nested_card(
    forwarded_contact_app,
):
    app, fixture = forwarded_contact_app
    page = query_contact_analysis(fixture["store"], forwarded_fixtures.CID)
    item = next(row for row in page["items"] if row["component_path"] == [3, 1])
    client = app.test_client()
    response = client.get("/browser/wechat/messages/" + item["id"])
    assert response.status_code == 302
    location = urlsplit(response.headers["Location"])
    query = parse_qs(location.query)
    assert location.fragment == item["anchor_id"]
    assert query["account"] == [item["account_key"]]
    assert query["session"] == [item["conversation_id"]]
    source = get_verified_browser_page(client, response.headers["Location"]).get_data(as_text=True)
    assert 'id="' + item["anchor_id"] + '"' in source
    assert "TARGET-NESTED-own reply only" in source


def test_forwarded_contact_table_labels_the_occurrence_and_searches_its_own_words(
    forwarded_contact_app,
):
    app, _ = forwarded_contact_app
    response = app.test_client().get(
        "/browser/wechat/contacts/" + forwarded_fixtures.CID,
        query_string={"q": "TARGET-NESTED"},
    )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Forwarded message" in html
    assert "TARGET-NESTED-own reply only" in html
    assert "QUOTED-ONLY-other" not in html
    assert "DESCENDANT-ONLY-another" not in html
