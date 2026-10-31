"""Contact identity metadata remains inside the existing compact disclosure.

Code version: v1.0.1-codex.0
"""

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader
import pytest


class MessageDisclosure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_details = False
        self.in_summary = False
        self.details_open = []
        self.summary = []
        self.metadata = []

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.in_details = True
            self.details_open.append("open" in dict(attrs))
        elif tag == "summary":
            self.in_summary = True

    def handle_endtag(self, tag):
        if tag == "summary":
            self.in_summary = False
        elif tag == "details":
            self.in_details = False

    def handle_data(self, data):
        if self.in_summary:
            self.summary.append(data)
        elif self.in_details:
            self.metadata.append(data)


@pytest.mark.parametrize("status", ["verified", "unresolved", "source_scoped"])
@pytest.mark.parametrize("namespace", ["native_user_name", "unverified_export_field"])
def test_contact_fields_are_escaped_and_do_not_add_a_closed_card_row(status, namespace):
    templates = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
    environment = Environment(loader=FileSystemLoader(templates), autoescape=True)
    context = {
        "kind": "group",
        "account_label": "Synthetic source",
        "account_id": "",
        "identity_status": "Source account unconfirmed",
        "source_label": "Text export",
        "source_file": "Synthetic archive",
        "peer_id": "",
        "sender_id": "",
        "sender_identifier_namespace": namespace,
        "direction": "unknown",
        "timestamp_label": "Unknown time",
        "time_basis": "Unavailable source time",
        "quality_flags": [],
        "provenance": {"locator": {}, "server_id": None, "source_time": None},
        "contact": {"id": "wechat:contact:<synthetic>", "identity_status": status},
    }
    current = SimpleNamespace(
        messaging=context,
        conversation_title="Synthetic room",
        stable_id="synthetic-room",
    )
    message = SimpleNamespace(
        messaging=context,
        stable_id="synthetic-message",
        author_label="Original source name",
        last_seen_at="",
        content_text="Original text",
    )
    page = SimpleNamespace(
        session_detail=True, current_session=current, items=(message,)
    )
    filters = SimpleNamespace(
        account="", chat_kind="group", sort="newest", session_page=1
    )
    rendered = environment.get_template("_wechat_browser.html").render(
        text_page=page, filters=filters, url_for=lambda *_args, **_kwargs: "/synthetic"
    )
    parsed = MessageDisclosure()
    parsed.feed(rendered)
    assert parsed.details_open == [False]
    assert "Original source name" in "".join(parsed.summary)
    assert "Contact ID" not in "".join(parsed.summary)
    assert "Contact ID" in "".join(parsed.metadata)
    identifier_label = (
        "Original export identifier"
        if namespace == "unverified_export_field"
        else "Sender ID"
    )
    assert identifier_label in "".join(parsed.metadata)
    assert status.replace("_", " ") in "".join(parsed.metadata)
    assert "wechat:contact:&lt;synthetic&gt;" in rendered
    assert "wechat:contact:<synthetic>" not in rendered
