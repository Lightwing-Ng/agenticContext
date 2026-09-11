"""Route and asset coverage for the formal Zhihu cache source.

Code version: v1.2.0-codex.1
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from app.core.chat_history_browser import query_chat_history
from app.core.config import CrawlConfig, load_saved_config, save_config
from app.core.zhihu_answers import (
    normalize_zhihu_answer_payload,
    normalize_zhihu_profile_url,
)
from app.core.zhihu_history import ZhihuHistoryStore, zhihu_history_path
from app.web.app import create_app


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ZHIHU_LOGO_ASSET = REPOSITORY_ROOT / "app/web/static/images/zhihu.svg"


def _create_zhihu_app(tmp_path: Path):
    with patch(
        "app.web.app.load_saved_config",
        return_value=CrawlConfig(zhihu_browser="edge"),
    ):
        return create_app(tmp_path / "local_store")


def test_zhihu_cache_page_uses_edge_login_and_text_first_controls(tmp_path: Path) -> None:
    application = _create_zhihu_app(tmp_path)
    body = application.test_client().get("/cache/zhihu").get_data(as_text=True)

    assert 'data-cache-source="zhihu"' in body
    assert 'aria-label="Cache source: Zhihu"' in body
    assert "data-cache-content-mode" in body
    assert 'data-platform="zhihu"' in body
    assert 'name="zhihu_browser"' in body
    assert 'value="edge"' in body
    assert 'data-browser-option="edge"' in body
    assert 'data-browser-option="chrome"' in body
    assert 'data-browser-option="safari"' not in body
    assert 'name="zhihu_author_url"' in body
    assert 'class="shared-select-text-input"' in body
    assert 'aria-describedby="zhihu_author_url_help"' not in body
    assert "Leave blank to cache answers upvoted by the signed-in account." not in body
    assert ">Open Zhihu settings</a>" in body
    assert 'href="/settings#settings-downloads"' in body
    assert "Images and rich-text destinations are retained only as source links" in body
    assert "--cache-source-mark: url('/static/images/zhihu.svg')" in body


def test_zhihu_routes_probe_and_dispatch_the_selected_edge_session(tmp_path: Path) -> None:
    application = _create_zhihu_app(tmp_path)
    client = application.test_client()
    service = application.extensions["zhihu_history_service"]

    with patch(
        "app.web.app.probe_browser_session",
        return_value={"logged_in": True, "can_download": True, "account_handle": "MayukoSF"},
    ) as probe:
        payload = client.get("/api/browser-session?platform=zhihu&browser=edge").get_json()
    assert payload["can_download"] is True
    assert payload["account_handle"] == "MayukoSF"
    assert probe.call_args.args[:2] == ("zhihu", "edge")

    with patch.object(service, "start") as start, patch("app.web.app.save_config"):
        response = client.post(
            "/cache/zhihu/start",
            data={
                "cache_content_mode": "text",
                "zhihu_browser": "edge",
                "zhihu_author_url": "https://www.zhihu.com/people/wang-xian-nan-da-tian-cai",
            },
        )
    assert response.status_code == 302
    assert start.call_args.args[0].zhihu_browser == "edge"
    assert start.call_args.kwargs == {
        "author_url": "https://www.zhihu.com/people/wang-xian-nan-da-tian-cai"
    }

    with patch(
        "app.web.app.open_zhihu_browser_for_login",
        return_value={"opened": True, "platform": "zhihu", "browser": "edge"},
    ) as open_login:
        opened = client.post(
            "/api/browser-session/open-login",
            json={"platform": "zhihu", "browser": "edge"},
        ).get_json()
    assert opened["opened"] is True
    open_login.assert_called_once()


def test_legacy_zhihu_route_and_status_use_the_formal_cache(tmp_path: Path) -> None:
    application = _create_zhihu_app(tmp_path)
    client = application.test_client()

    redirect_response = client.get("/zhihu?from=legacy")
    status = client.get("/api/zhihu/status").get_json()

    assert redirect_response.status_code == 302
    assert redirect_response.headers["Location"].endswith("/cache/zhihu?from=legacy")
    assert status["downloaded_posts"] == 0
    assert status["progress_unit"] == "answers"
    assert status["output_dir"].endswith("/llm/zhihu")


def test_local_resources_renders_zhihu_text_and_links_without_remote_media(tmp_path: Path) -> None:
    application = _create_zhihu_app(tmp_path)
    profile = normalize_zhihu_profile_url("https://www.zhihu.com/people/fixture-author")
    answer = normalize_zhihu_answer_payload(
        {
            "id": 2197549311,
            "content": (
                '<p>Cached answer body</p><a href="https://example.com/reference">Reference</a>'
                '<img data-original="https://pic1.zhimg.com/example.png">'
                '<p>Full answer final sentence.</p>'
            ),
            "content_need_truncated": True,
            "author": {
                "id": "author-id",
                "name": "Fixture Author",
                "url_token": "fixture-author",
            },
            "question": {"id": 495309288, "title": "Fixture question"},
        },
        profile,
    )
    other_profile = normalize_zhihu_profile_url(
        "https://www.zhihu.com/people/other-author"
    )
    other_answer = normalize_zhihu_answer_payload(
        {
            "id": 2200000000,
            "content": "<p>Other cached answer.</p>",
            "author": {
                "id": "other-author-id",
                "name": "Other Author",
                "url_token": "other-author",
            },
            "question": {"id": 495309289, "title": "Other fixture question"},
        },
        other_profile,
    )
    store = ZhihuHistoryStore(zhihu_history_path(tmp_path / "local_store"))
    store.merge_answers((answer, other_answer), "2026-09-11T00:00:00Z")
    store.save()

    client = application.test_client()
    body = client.get("/browser?view=text&source=zhihu&session_view=0").get_data(
        as_text=True
    )
    session_page = query_chat_history(
        tmp_path / "local_store",
        source="zhihu",
        session_view=True,
    )
    session = session_page.sessions[0]
    index_body = client.get(
        "/browser?view=text&source=zhihu&session_view=1"
    ).get_data(as_text=True)
    filtered_index_body = client.get(
        "/browser?view=text&source=zhihu&session_view=1&answerer=Fixture+Author"
    ).get_data(as_text=True)
    detail_body = client.get(
        "/browser?view=text&source=zhihu&session_view=1"
        f"&answerer=Fixture+Author&session={session.stable_id}"
    ).get_data(as_text=True)

    assert "Cached answer body" in body
    assert "Full answer final sentence." in body
    assert 'src="/static/images/zhihu.svg"' in body
    assert 'href="https://example.com/reference"' in body
    assert 'href="https://pic1.zhimg.com/example.png"' in body
    assert 'src="https://pic1.zhimg.com/example.png"' not in body
    assert (
        '<th scope="col" class="browser-session-col-count '
        'browser-session-col-author">Answerer</th>'
    ) in index_body
    assert '<td class="browser-session-table-author">Fixture Author</td>' in index_body
    assert '<option value="">All answerers</option>' in index_body
    assert '<option value="Fixture Author"' in index_body
    assert '<option value="Other Author"' in index_body
    assert 'class="secondary-button browser-clear-link"' not in index_body
    assert "Other fixture question" in index_body
    assert "Fixture question" in filtered_index_body
    assert "Other fixture question" not in filtered_index_body
    assert (
        '<option value="Fixture Author" selected>Fixture Author</option>'
        in filtered_index_body
    )
    assert '<span class="browser-session-table-id">2197549311</span>' not in index_body
    assert '<span class="metric-label">Projects</span>' not in index_body
    assert (
        'class="browser-session-col-source scrollable-data-table-filter-header"'
        not in index_body
    )
    assert 'class="browser-session-table-source"' not in index_body
    assert "Full answer final sentence." in detail_body
    assert "browser-session-table-message-shell is-expanded" in detail_body
    assert "data-browser-session-message-toggle" not in detail_body
    assert (
        '<th scope="col" class="browser-session-col-role">Fixture Author</th>'
        in detail_body
    )
    assert '<span class="metric-label">Projects</span>' not in detail_body


def test_zhihu_browser_preference_round_trips(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"

    save_config(CrawlConfig(zhihu_browser="chrome"), settings_path)

    assert load_saved_config(settings_path).zhihu_browser == "chrome"


def test_zhihu_logo_asset_is_exactly_the_first_vector_character() -> None:
    asset_root = ElementTree.parse(ZHIHU_LOGO_ASSET).getroot()
    asset_paths = asset_root.findall("{http://www.w3.org/2000/svg}path")

    assert len(asset_paths) == 1
    assert hashlib.sha256(asset_paths[0].attrib["d"].encode()).hexdigest() == (
        "983f7e2f6a2c6191f183f16afeded198d745694ad95feeb5b9ea8edc6584ed2e"
    )
    assert asset_root.attrib["viewBox"] == "0 0 92 91"
    assert asset_paths[0].attrib["fill"] == "#0f88eb"
