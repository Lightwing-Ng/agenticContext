"""Preferred source identity stays visible beside retained observation counts.

Code version: v1.0.0-codex.0
"""

from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
import pytest


class StatementCells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells = []
        self.cell = None
        self.note_count = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "td":
            self.cell = []
        if tag == "span" and self.cell is not None:
            if attributes.get("class") == "wechat-contact-cell-note":
                self.note_count += 1

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.cells.append("".join(self.cell).strip())
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


@pytest.mark.parametrize(
    ("label", "account", "count", "expected_account", "expected_note"),
    [
        (
            "Native owner · 6mwi12",
            "wxid_owner",
            2,
            "Native owner · 6mwi12",
            "iPad archive · 2 sources",
        ),
        ("", "wxid_owner", 1, "wxid_owner", "iPad archive"),
        ("", "", 1, "Unconfirmed source account", "iPad archive"),
    ],
)
def test_source_cell_names_the_preferred_source_without_extra_note_rows(
    label, account, count, expected_account, expected_note
):
    templates = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
    environment = Environment(loader=FileSystemLoader(templates), autoescape=True)
    item = {
        "id": "preferred-message",
        "account_key": "account:wxid_owner",
        "source_id": "ios-preferred",
        "source_url": "/synthetic-source",
        "content_text": "Literal statement",
        "timestamp_value": "2026-11-01T10:00:00+08:00",
        "timestamp_label": "1 Nov 2026 10:00:00",
        "kind": "direct",
        "conversation_title": "Synthetic conversation",
        "viewer_account_label": label,
        "account_id": account,
        "source_label": "iPad archive",
        "source_file": "Preserved native snapshot",
        "is_embedded": False,
        "observation_count": count,
        "observations": [],
    }
    rendered = environment.get_template("_wechat_contact_analysis_content.html").render(
        analysis={
            "status": "ready",
            "contact": {"id": "synthetic-contact"},
            "total_count": 1,
            "conversation_count": 1,
            "account_count": 1,
            "current_page": 1,
            "page_size": 50,
            "items": [item],
        },
        contact_filters={"q": "", "sort": "newest"},
        return_url="",
        pagination_items=[],
        url_for=lambda *_args, **_kwargs: "/synthetic",
    )
    parsed = StatementCells()
    parsed.feed(rendered)
    assert len(parsed.cells) == 5
    assert parsed.cells[-1] == expected_account + expected_note
    assert parsed.note_count == 1
    assert 'title="Preserved native snapshot"' in rendered
    assert 'href="/synthetic-source" data-wechat-contact-statement' in rendered
    assert ('aria-label="Show original sources"' in rendered) == (count > 1)
