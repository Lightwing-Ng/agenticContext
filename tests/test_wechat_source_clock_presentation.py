"""Source-clock presentation preserves literal wall clocks and source precision.

Code version: v1.1.0-codex.0
"""

from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, render_template
import pytest

from app.web.presentation import format_wechat_source_clock


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2021-6-2 下午12:47", "2 Jun 2021 12:47"),
        ("2021-6-2 下午1:07", "2 Jun 2021 13:07"),
        ("2021-6-2 上午12:47", "2 Jun 2021 00:47"),
        ("2021-6-2 凌晨12:47", "2 Jun 2021 00:47"),
        ("2021-6-2 凌晨0:47", "2 Jun 2021 00:47"),
        ("2021-6-2 中午12:47", "2 Jun 2021 12:47"),
        ("2021-6-2 12:47 AM", "2 Jun 2021 00:47"),
        ("2021-6-2 12:47 PM", "2 Jun 2021 12:47"),
        ("2021-6-2 1:07 pm", "2 Jun 2021 13:07"),
        ("2021-6-2 23:47", "2 Jun 2021 23:47"),
        ("2021-6-2 下午12:47:08", "2 Jun 2021 12:47:08"),
        ("2021-06-02 12:47:00", "2 Jun 2021 12:47:00"),
        ("2024-2-29 上午1:02", "29 Feb 2024 01:02"),
        ("2021-06-02T12:47", "2 Jun 2021 12:47"),
        ("2021-06-02T12:47:08", "2 Jun 2021 12:47:08"),
        ("2021-06-02T12:47+08:00", "2 Jun 2021 12:47 UTC+08:00"),
        ("2021-06-02T12:47:08-05:30", "2 Jun 2021 12:47:08 UTC-05:30"),
        ("2021-06-02T12:47Z", "2 Jun 2021 12:47 UTC"),
        (
            "2021-06-02T12:47:08.1200+08:00",
            "2 Jun 2021 12:47:08.1200 UTC+08:00",
        ),
    ],
)
def test_source_clock_display_keeps_literal_clock_and_precision(raw, expected):
    assert format_wechat_source_clock(raw) == expected
    assert format_wechat_source_clock(expected) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "Unknown original time",
        "0",
        "1546300800",
        "1970-01-01 08:00:00",
        "1970-1-1 8:00 AM",
        "2021-06-02",
        "2021-02-29 下午12:47",
        "2021-6-2 下午0:47",
        "2021-6-2 下午18:47",
        "2021-6-2 中午1:47",
        "2021-6-2 凌晨11:47",
        "2021-6-2 下午12:47 AM",
        "2021-6-2 24:47",
        "2021-6-2 12:60",
        "2021-6-2 12:47:60",
        "2021-06-02T12:47:08+08:99",
        "2021-06-02T12:47:08+01:60",
        "2021-06-02T12:47:08+24:00",
        "昨日 下午12:47",
        "2 Jun 2021 12:47",
        "not a source clock <script>literal</script>",
    ],
)
def test_unknown_or_invalid_source_clock_is_preserved_as_evidence(raw):
    assert format_wechat_source_clock(raw) == raw


@pytest.mark.parametrize("raw", [None, ""])
def test_absent_source_clock_has_no_invented_value(raw):
    assert format_wechat_source_clock(raw) == ""


class _RenderedProvenance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.parts = []
        self.label = ""
        self.fields = {}
        self.times = []
        self.source_clocks = []

    def handle_starttag(self, tag, attrs):
        if tag in {"dt", "dd", "time"}:
            self.current = tag
            self.parts = []
        elif tag == "span" and "data-wechat-contact-source-clock" in dict(attrs):
            self.current = "source_clock"
            self.parts = []

    def handle_data(self, data):
        if self.current:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag != ("span" if self.current == "source_clock" else self.current):
            return
        text = "".join(self.parts)
        if tag == "dt":
            self.label = text
        elif tag == "dd":
            self.fields[self.label] = text
        elif tag == "time":
            self.times.append(text)
        elif self.current == "source_clock":
            self.source_clocks.append(text)
        self.current = None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2021-6-2 下午12:47", "2 Jun 2021 12:47"),
        ("2021-6-2 上午12:47", "2 Jun 2021 00:47"),
        ("2021-6-2 下午12:47:08", "2 Jun 2021 12:47:08"),
        (
            "<script>unrecognized original clock</script>",
            "<script>unrecognized original clock</script>",
        ),
    ],
)
def test_chat_disclosure_formats_source_clock_without_mutating_raw_context(
    raw, expected
):
    templates = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
    app = Flask(__name__, template_folder=str(templates))
    app.jinja_env.filters["wechat_source_clock"] = format_wechat_source_clock
    context = {
        "kind": "direct",
        "account_label": "Synthetic source",
        "account_id": "wxid_synthetic_owner",
        "identity_status": "Verified archive ID",
        "source_label": "Native archive",
        "source_file": "Synthetic native source",
        "peer_id": "wxid_synthetic_contact",
        "sender_id": "wxid_synthetic_contact",
        "direction": "received",
        "timestamp_label": "2 Jun 2021 12:47:08",
        "time_basis": "UTC+08:00",
        "quality_flags": [],
        "provenance": {"source_time": raw, "locator": {}},
    }
    original = deepcopy(context)
    message = SimpleNamespace(
        messaging=context,
        stable_id="synthetic-source-clock",
        author_label="Synthetic contact",
        last_seen_at="2021-06-02T12:47:08+08:00",
        content_text="Original source text",
    )
    page = SimpleNamespace(
        session_detail=True,
        current_session=SimpleNamespace(messaging=context),
        items=(message,),
    )
    with app.test_request_context("/"):
        rendered = render_template(
            "_wechat_browser.html",
            text_page=page,
            filters=SimpleNamespace(
                account="", chat_kind="direct", sort="newest", session_page=1
            ),
            url_for=lambda *_args, **_kwargs: "/synthetic",
        )
    parsed = _RenderedProvenance()
    parsed.feed(rendered)
    assert parsed.fields["Source clock"] == expected
    assert parsed.fields["Time basis"] == "UTC+08:00"
    assert parsed.times == ["2 Jun 2021 12:47:08"]
    assert context == original
    if "<script>" in raw:
        assert "<script>" not in rendered
        assert "&lt;script&gt;" in rendered


def _render_contact_source_clock(
    raw,
    *,
    timestamp_value="2021-06-02T12:47:08+08:00",
    timestamp_label="2 Jun 2021 12:47:08",
    display_timestamp_label=None,
):
    templates = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
    app = Flask(__name__, template_folder=str(templates))
    app.jinja_env.filters["wechat_source_clock"] = format_wechat_source_clock
    observation = {
        "id": "synthetic-source-observation",
        "account_key": "account:wxid_owner",
        "source_url": "/synthetic-source",
        "conversation_title": "Synthetic conversation",
        "account_id": "wxid_owner",
        "source_file": "Synthetic archive",
        "source_label": "Native archive",
        "timestamp_value": timestamp_value,
        "timestamp_label": timestamp_label,
        "display_timestamp_value": timestamp_value,
        "display_timestamp_label": display_timestamp_label or timestamp_label,
        "time_basis": "UTC+08:00",
        "source_clock": raw,
    }
    item = {
        **observation,
        "source_id": "synthetic-source",
        "content_text": "Unchanged original statement",
        "kind": "direct",
        "observation_count": 2,
        "observations": [
            observation,
            {
                **observation,
                "id": "independent-source-observation",
                "account_key": "account:wxid_second_owner",
                "account_id": "wxid_second_owner",
                "source_url": "/independent-synthetic-source",
                "source_clock": "",
            },
        ],
    }
    before = deepcopy(item)
    with app.test_request_context("/"):
        rendered = render_template(
            "_wechat_contact_analysis_content.html",
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
    assert item == before
    return rendered


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2021-6-2 下午12:47", "2 Jun 2021 12:47"),
        ("2021-06-02T12:47-05:30", "2 Jun 2021 12:47 UTC-05:30"),
        ("2021-06-02T12:47:08+09:00", "2 Jun 2021 12:47:08 UTC+09:00"),
        ("2021-06-02T12:47:08.1200", "2 Jun 2021 12:47:08.1200"),
        (
            "<script>unrecognized original clock</script>",
            "<script>unrecognized original clock</script>",
        ),
        ("Unknown original time", "Unknown original time"),
    ],
)
def test_contact_sources_reuse_formatting_without_changing_observation_fields(
    raw, expected
):
    rendered = _render_contact_source_clock(raw)
    parser = _RenderedProvenance()
    parser.feed(rendered)
    assert parser.source_clocks == ["Source clock: " + expected]
    assert parser.times == ["2 Jun 2021 12:47:08"] * 3
    if "<script>" in raw:
        assert "<script>" not in rendered
        assert "&lt;script&gt;" in rendered


@pytest.mark.parametrize(
    ("raw", "timestamp_value", "timestamp_label", "display_label"),
    [
        (
            "2021-06-02 12:47:08",
            "2021-06-02T12:47:08",
            "2 Jun 2021 12:47:08",
            "2 Jun 2021 12:47:08",
        ),
        (
            "2021-6-2 下午12:47:08",
            "2021-06-02T12:47:08",
            "2 Jun 2021 12:47:08",
            "2 Jun 2021 12:47:08",
        ),
        (
            "2021-6-2 下午12:47",
            "2021-06-02T12:47",
            "2 Jun 2021 12:47",
            "2 Jun 2021 12:47",
        ),
        (
            "2021-06-02T12:47",
            "2021-06-02 12:47",
            "2 Jun 2021 12:47",
            "2 Jun 2021 12:47",
        ),
        (
            "2021-06-02 12:47:08",
            "2021-06-02T12:47:08",
            "Legacy source label",
            "2 Jun 2021 12:47:08",
        ),
        (
            "2021-06-02T12:47:08+08:00",
            "2021-06-02T12:47:08+08:00",
            "2 Jun 2021 12:47:08",
            "2 Jun 2021 12:47:08",
        ),
    ],
)
def test_contact_sources_do_not_repeat_the_same_formatted_clock(
    raw, timestamp_value, timestamp_label, display_label
):
    rendered = _render_contact_source_clock(
        raw,
        timestamp_value=timestamp_value,
        timestamp_label=timestamp_label,
        display_timestamp_label=display_label,
    )
    parser = _RenderedProvenance()
    parser.feed(rendered)
    assert parser.source_clocks == []
    assert parser.times[-2:] == [display_label, display_label]
    assert 'aria-label="Show original sources"' in rendered


@pytest.mark.parametrize("raw", ["0", "1970-01-01 08:00:00", "Unknown original time"])
def test_unknown_contact_clock_remains_inspectable_without_an_invented_time(raw):
    rendered = _render_contact_source_clock(
        raw, timestamp_value="", timestamp_label="Unknown time"
    )
    parser = _RenderedProvenance()
    parser.feed(rendered)
    assert parser.times == []
    assert parser.source_clocks == ["Source clock: " + raw]
