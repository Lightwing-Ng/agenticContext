"""Nested contact pages retain Local resources navigation without loading its data.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import json
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from flask import template_rendered
import pytest

from app.core.wechat_contacts import canonical_contact_id
from app.core.wechat_display_names import WechatDisplayNames, display_names_path
from app.web import local_resource_navigation as navigation
from tests import test_wechat_contact_analysis_routes as fixtures


contact_app = fixtures.contact_app
contact_analysis_store = fixtures.contact_analysis_store
CID = fixtures.CID


@contextmanager
def _rendered_context(app):
    contexts = []

    def record(_sender, template, context, **_extra):
        if template.name == "wechat_contact_analysis.html":
            contexts.append(context)

    template_rendered.connect(record, app, weak=False)
    try:
        yield contexts
    finally:
        template_rendered.disconnect(record, app)


def test_contact_keeps_the_origin_filters_and_exact_return_location(contact_app):
    app, fixture = contact_app
    origin = (
        "/browser?view=text&source=wechat&account=account%3Awxid_account_a"
        "&chat_kind=group&session=wechat%3Aroom&page=3&session_page=7"
        "&session_view=1&q=original+search&sort=oldest#wechat_message_saved"
    )
    with (
        _rendered_context(app) as captured,
        patch(
            "app.web.local_resource_routes.query_chat_history",
            side_effect=AssertionError("No browser data query"),
        ),
        patch.object(
            app.extensions["local_media_catalog"],
            "snapshot",
            side_effect=AssertionError("No media scan"),
        ),
    ):
        response = app.test_client().get(
            "/browser/wechat/contacts/" + CID,
            query_string={"return_to": origin, "sort": "newest", "q": "contact search"},
        )
    assert response.status_code == 200
    context = captured[0]
    assert context["return_url"] == origin
    filters = context["filters"]
    assert {
        key: filters[key]
        for key in (
            "view",
            "source",
            "account",
            "chat_kind",
            "session",
            "page",
            "session_page",
            "q",
            "sort",
        )
    } == {
        "view": "text",
        "source": "wechat",
        "account": "account:wxid_account_a",
        "chat_kind": "group",
        "session": "wechat:room",
        "page": 3,
        "session_page": 7,
        "q": "original search",
        "sort": "oldest",
    }
    assert context["contact_filters"] == {"q": "contact search", "sort": "newest"}
    assert {source.key for source in context["local_resource_text_sources"]} >= {
        "wechat",
        "zhihu",
    }
    assert tuple(
        source.key for source in context["local_resource_text_sources"]
    ) == tuple(
        source.key
        for source in navigation.local_resource_source_context()[
            "local_resource_text_sources"
        ]
    )
    assert any(
        row["key"] == "account:wxid_account_a"
        for row in context["text_page"].messaging["accounts"]
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://outside.invalid/browser?account=wrong",
        "/settings",
        "/browser/wechat/contacts/recursive",
    ],
)
def test_invalid_origin_uses_the_standard_wechat_navigation_default(
    contact_app, origin
):
    app, _ = contact_app
    with _rendered_context(app) as captured:
        response = app.test_client().get(
            "/browser/wechat/contacts/" + CID, query_string={"return_to": origin}
        )
    assert response.status_code == 200
    context = captured[0]
    assert context["filters"]["source"] == "wechat"
    assert context["filters"]["account"] == ""
    assert context["filters"]["session"] == ""
    returned = urlsplit(context["return_url"])
    assert returned.path == "/browser" and not returned.netloc
    assert "return_to" not in parse_qs(returned.query)


def test_partition_navigation_queries_only_metadata_and_reuses_account_projection(
    contact_analysis_store,
):
    fixture = contact_analysis_store
    existing, _ = navigation._wechat_account_navigation(fixture.store)
    hidden = next(
        row["partition_id"]
        for row in existing
        if row["key"] != "account:wxid_account_a"
    )
    owner = canonical_contact_id("wxid_account_a", authority="native_account_owner")
    display_names_path(fixture.store).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "contacts": {owner: "Source owner"},
                "hidden_account_sources": [hidden],
            }
        )
    )
    statements = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    before = fixture.history.read_bytes()
    with patch(
        "app.web.local_resource_navigation.sqlite3.connect", side_effect=traced_connect
    ):
        accounts, options = navigation._wechat_account_navigation(fixture.store)
    assert statements and all("FROM partitions" in sql for sql in statements)
    assert not any("messages" in sql or "conversations" in sql for sql in statements)
    assert any(row["partition_id"] == hidden for row in accounts)
    assert all(row["partition_id"] != hidden for row in options)
    assert any(
        row["key"] == "account:wxid_account_a"
        and row["viewer_name"] == "Source owner · ount_a"
        for row in options
    )
    assert before == fixture.history.read_bytes()


def test_optional_cloud_preference_does_not_download_during_navigation():
    preferences = MagicMock()
    preferences.parent.is_symlink.return_value = False
    preferences.parent.parent.is_symlink.return_value = False
    preferences.is_symlink.return_value = False
    preferences.stat.return_value = SimpleNamespace(
        st_mode=stat.S_IFREG, st_flags=0x40000000
    )
    with patch(
        "app.core.wechat_display_names.display_names_path", return_value=preferences
    ):
        names = WechatDisplayNames("unused-local-root")
    preferences.open.assert_not_called()
    assert names.names == {}


def test_busy_archive_keeps_sources_available_without_opening_metadata(contact_app):
    app, fixture = contact_app
    fixture.history.with_name(fixture.history.name + "-journal").write_bytes(b"busy")
    with _rendered_context(app) as captured:
        response = app.test_client().get(
            "/browser/wechat/contacts/" + CID,
            query_string={
                "return_to": "/browser?view=text&source=wechat&account=account%3Awxid_account_a"
            },
        )
    assert response.status_code == 202
    context = captured[0]
    assert context["text_page"].messaging["accounts"] == ()
    assert context["filters"]["account"] == "account:wxid_account_a"
    assert {source.key for source in context["local_resource_text_sources"]} >= {
        "wechat",
        "zhihu",
    }


def test_json_contact_poll_does_not_rebuild_sidebar_navigation(contact_app):
    app, _ = contact_app
    with patch(
        "app.web.local_resource_routes.wechat_contact_sidebar_context",
        side_effect=AssertionError("Poll only needs its content fragment"),
    ):
        response = app.test_client().get(
            "/browser/wechat/contacts/" + CID, headers={"Accept": "application/json"}
        )
    assert response.status_code == 200
    assert response.json["status"] == "ready"
