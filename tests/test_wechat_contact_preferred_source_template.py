"""Merged statements stay compact while source-clock conflicts remain inspectable.

Code version: v1.2.1-codex.0
"""

from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
import pytest

from app.web.presentation import format_wechat_source_clock


class StatementCells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells = []
        self.cell = None
        self.note_counts = []
        self.cell_note_count = 0
        self.source_disclosures = []
        self.source_titles = []
        self.observation_ids = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "details" and "data-wechat-contact-sources" in attributes:
            self.source_disclosures.append("open" in attributes)
        if tag == "summary" and attributes.get("aria-label") == "Show original sources":
            self.source_titles.append(attributes.get("title"))
        if "data-wechat-contact-observation" in attributes:
            self.observation_ids.append(attributes["data-wechat-contact-observation"])
        if tag == "td":
            self.cell = []
            self.cell_note_count = 0
        if tag == "span" and self.cell is not None:
            if attributes.get("class") == "wechat-contact-cell-note":
                self.cell_note_count += 1

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.cells.append("".join(self.cell).strip())
            self.note_counts.append(self.cell_note_count)
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


@pytest.mark.parametrize(
    (
        "label",
        "account",
        "count",
        "conflict",
        "uncertain",
        "expected_account",
        "expected_note",
    ),
    [
        (
            "Native owner · 6mwi12",
            "wxid_owner",
            2,
            False,
            False,
            "Native owner · 6mwi12",
            "iPad archive",
        ),
        ("", "wxid_owner", 1, False, False, "wxid_owner", "iPad archive"),
        ("", "", 1, False, False, "Unconfirmed source account", "iPad archive"),
        (
            "",
            "wxid_owner",
            100,
            False,
            False,
            "wxid_owner",
            "iPad archive",
        ),
        ("", "wxid_owner", 2, True, False, "wxid_owner", "iPad archive · 2 sources"),
        ("", "wxid_owner", 2, False, True, "wxid_owner", "iPad archive · 2 sources"),
        ("", "wxid_owner", 1, True, False, "wxid_owner", "iPad archive · 1 source"),
        ("", "wxid_owner", 1, False, True, "wxid_owner", "iPad archive · 1 source"),
    ],
)
def test_source_cell_names_the_preferred_source_without_extra_note_rows(
    label, account, count, conflict, uncertain, expected_account, expected_note
):
    templates = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
    environment = Environment(loader=FileSystemLoader(templates), autoescape=True)
    environment.filters["wechat_source_clock"] = format_wechat_source_clock
    observations = [
        {
            "id": f"native-observation-{index}",
            "account_key": f"account:wxid_owner_{index % 2}",
            "source_url": f"/synthetic-source/{index}",
            "conversation_title": "Same native group",
            "viewer_account_label": f"Native owner {index % 2}",
            "account_id": f"wxid_owner_{index % 2}",
            "source_file": "Preserved native snapshot",
            "source_label": "iPad archive",
            "reference_kind": "primary",
            "timestamp_value": "2026-11-01T10:00:00+08:00",
            "timestamp_label": "1 Nov 2026 10:00:00",
            "display_timestamp_value": "2026-11-01T10:00:00+08:00",
            "display_timestamp_label": "1 Nov 2026 10:00:00",
            "time_basis": "UTC+08:00",
            "source_clock": "",
        }
        for index in range(count)
    ]
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
        "observations": observations,
        "time_conflict": conflict,
        "time_uncertain": uncertain,
        "has_observed_time": True,
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
    assert parsed.note_counts[-1] == 1
    assert 'title="Preserved native snapshot"' in rendered
    assert 'href="/synthetic-source" data-wechat-contact-statement' in rendered
    has_sources = count > 1 or conflict or uncertain
    assert ('aria-label="Show original sources"' in rendered) == has_sources
    assert ('class="wechat-contact-statement-entry has-sources"' in rendered) == (
        has_sources
    )
    assert parsed.source_disclosures == ([False] if has_sources else [])
    expected_title = "Show original sources"
    if count > 1:
        expected_title += f" · {count:,} sources"
    assert parsed.source_titles == ([expected_title] if has_sources else [])
    assert parsed.observation_ids == (
        [observation["id"] for observation in observations] if has_sources else []
    )
    if has_sources:
        assert all(
            f'href="{observation["source_url"]}" data-wechat-contact-source' in rendered
            for observation in observations
        )
