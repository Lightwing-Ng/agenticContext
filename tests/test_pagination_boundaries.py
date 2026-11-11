"""Keep shared pagination controls reachable and range navigation local.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from flask import Flask, render_template_string
import pytest

from app.core.local_media_browser import build_local_store_pagination


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("total", [6, 11, 16, 51])
@pytest.mark.parametrize("position", ["first", "last"])
def test_one_remaining_page_is_a_direct_link_without_a_range(total, position):
    current = total - (5 if position == "first" else 1)
    items = build_local_store_pagination(total, current)

    assert not any(
        item.kind == "ellipsis" and item.position == "trailing" for item in items
    )
    assert [item.page for item in items if item.kind == "page"].count(total) == 1
    assert [item.page for item in items if item.kind == "next"] == [total]
    assert [item.page for item in items if item.is_active] == [current]


def test_all_chunks_keep_reachable_pages_and_never_offer_a_singleton_range():
    for total in range(2, 101):
        for current in range(1, total + 1):
            items = build_local_store_pagination(total, current)
            direct = [item.page for item in items if item.kind == "page"]
            assert len(direct) == len(set(direct)), (total, current)
            assert [item.page for item in items if item.is_active] == [current]
            reachable = set(direct)
            for item in items:
                if item.kind != "ellipsis":
                    continue
                assert item.ranges
                for first, last in item.ranges:
                    assert 1 <= first < last <= total, (total, current, first, last)
                    reachable.update(range(first, last + 1))
            assert reachable == set(range(1, total + 1)), (total, current)


@pytest.mark.parametrize(("total", "expected"), [(7, ((6, 7),)), (12, ((6, 12),))])
def test_multiple_remaining_pages_keep_the_existing_chunk_ranges(total, expected):
    items = build_local_store_pagination(total, 1)
    (trailing,) = [item for item in items if item.kind == "ellipsis"]
    assert trailing.position == "trailing"
    assert trailing.ranges == expected


class _Navigation(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = []

    def handle_starttag(self, tag, attrs):
        if tag in {"a", "button", "span"}:
            self.controls.append((tag, dict(attrs)))


@pytest.mark.parametrize("view", ["text", "media", "prompts"])
def test_rendered_page_ten_of_eleven_has_no_redundant_later_menu(view):
    app = Flask(__name__, template_folder=str(ROOT / "app/web/templates"))
    app.add_url_rule(
        "/browser", endpoint="local_resources.browser", view_func=lambda: ""
    )
    page = SimpleNamespace(
        pagination_items=build_local_store_pagination(11, 10),
        current_page=10,
        session_detail=view == "text",
        session_view=False,
        pagination_unit="page",
    )
    filters = SimpleNamespace(
        source="wechat" if view == "text" else "x",
        account="account:fixture-owner",
        chat_kind="direct",
        project="",
        answerer="",
        q="",
        sort="oldest",
        session_view=False,
        session="wechat:fixture-conversation",
        session_page=1,
        kind="all",
        session_index=False,
        media_view="",
    )
    with app.test_request_context("/browser"):
        body = render_template_string(
            '{% from "_pagination.html" import render_browser_pagination %}'
            '{{ render_browser_pagination(page, "Fixture pages", view, filters) }}',
            page=page,
            filters=filters,
            view=view,
        )
    navigation = _Navigation()
    navigation.feed(body)

    assert not any(
        attrs.get("data-pagination-ellipsis") == "trailing"
        for _, attrs in navigation.controls
    )
    assert not any(
        attrs.get("aria-label", "").startswith("Show later")
        for _, attrs in navigation.controls
    )
    direct_last = [
        attrs
        for tag, attrs in navigation.controls
        if tag == "a"
        and attrs.get("data-pagination-target") == "11"
        and "local-store-page-nav" not in attrs.get("class", "").split()
    ]
    assert len(direct_last) == 1
    assert "page=11" in direct_last[0]["href"]
    assert "11-11" not in body
    assert any(
        attrs.get("data-pagination-ellipsis") == "leading"
        for _, attrs in navigation.controls
    )


@pytest.mark.parametrize(
    ("view", "range_prefix"),
    [("text", "Messages"), ("media", "Pages"), ("prompts", "Saved prompts")],
)
@pytest.mark.parametrize(
    ("current", "range_label", "expected"),
    [(821, "816 through 820", 820), (7, "11 through 15", 11)],
)
def test_range_option_links_open_the_nearest_page(
    view, range_prefix, current, range_label, expected
):
    app = Flask(__name__, template_folder=str(ROOT / "app/web/templates"))
    app.add_url_rule(
        "/browser", endpoint="local_resources.browser", view_func=lambda: ""
    )
    page = SimpleNamespace(
        pagination_items=build_local_store_pagination(821, current),
        current_page=current,
        session_detail=view == "text",
        session_view=True,
        pagination_unit="page",
    )
    filters = SimpleNamespace(
        source="wechat" if view == "text" else "fixture",
        account="account:fixture-owner",
        chat_kind="group",
        project="",
        answerer="",
        q="preserved query",
        sort="oldest",
        session_view=True,
        session="wechat:fixture-conversation",
        session_page=7,
        kind="all",
        session_index=False,
        media_view="",
    )
    with app.test_request_context("/browser"):
        body = render_template_string(
            '{% from "_pagination.html" import render_browser_pagination %}'
            '{{ render_browser_pagination(page, "Fixture pages", view, filters) }}',
            page=page,
            filters=filters,
            view=view,
        )
    navigation = _Navigation()
    navigation.feed(body)
    matching_links = [
        attrs
        for tag, attrs in navigation.controls
        if tag == "a"
        and attrs.get("aria-label") == f"{range_prefix} {range_label}"
    ]
    assert len(matching_links) == 1
    query = parse_qs(urlsplit(matching_links[0]["href"]).query)
    assert query["page"] == [str(expected)]
    if view == "text":
        assert query["account"] == [filters.account]
        assert query["chat_kind"] == [filters.chat_kind]
        assert query["session"] == [filters.session]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required.")
def test_agent_javascript_and_server_builder_keep_the_same_boundaries():
    cases = sorted(
        {
            (total, current)
            for total in range(2, 101)
            for current in {1, total, max(1, total - 1), max(1, total - 5)}
        }
        | {(457, 53)}
    )
    javascript = r"""
        const fs = require('node:fs');
        const vm = require('node:vm');
        const source = fs.readFileSync(process.argv[1], 'utf8');
        const first = source.indexOf('    function buildAgentPaginationItems(');
        const last = source.indexOf('    function agentPaginationRangeElements(', first);
        if (first < 0 || last <= first) throw new Error('Pagination functions missing.');
        const runtime = vm.runInNewContext(source.slice(first, last) +
            '\n({buildAgentPaginationItems, buildAgentPaginationRanges})');
        const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
        const results = cases.map(([total, current]) =>
            runtime.buildAgentPaginationItems(total, current).map(item => ({
                kind: item.kind,
                page: item.page || 0,
                active: Boolean(item.isActive),
                position: item.position || '',
                ranges: item.kind === 'ellipsis'
                    ? runtime.buildAgentPaginationRanges(item.firstPage, item.lastPage)
                    : [],
            })));
        process.stdout.write(JSON.stringify(results));
    """
    result = subprocess.run(
        [
            shutil.which("node"),
            "-e",
            javascript,
            str(ROOT / "app/web/static/computer-use-agent.js"),
        ],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    actual = json.loads(result.stdout)
    expected = [
        [
            {
                "kind": item.kind,
                "page": item.page,
                "active": item.is_active,
                "position": item.position,
                "ranges": [list(bounds) for bounds in item.ranges],
            }
            for item in build_local_store_pagination(total, current)
        ]
        for total, current in cases
    ]
    assert actual == expected
