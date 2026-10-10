"""Isolated Zhihu answerer update flow in Local resources.

The real service, task state, collector, and Parquet merge run against injected
Zhihu API pages, so no test contacts Zhihu or opens a signed-in browser profile.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest
from playwright.sync_api import Browser, Dialog, Page, expect
from werkzeug.serving import make_server

from app.core.chat_history_browser import query_chat_history
from app.core.zhihu_answers import (
    ZhihuVerificationRequiredError,
    normalize_zhihu_answer_payload,
    normalize_zhihu_profile_url,
)
from app.core.zhihu_history import (
    ZhihuHistoryStore,
    sync_zhihu_history as run_zhihu_history_sync,
    zhihu_history_path,
)
from tests import test_sidebar_e2e


disposable_browser = test_sidebar_e2e.disposable_browser

AUTHOR_TOKEN = "fixture-author"
AUTHOR_NAME = "Fixture Author"
PROFILE_URL = f"https://www.zhihu.com/people/{AUTHOR_TOKEN}"
UPDATE_LABEL = "Update latest answers from Zhihu"


def _answer_payload(answer_id: int, content: str | None = None) -> dict[str, object]:
    return {
        "id": answer_id,
        "excerpt": f"Answer {answer_id}",
        "content": content or f"<p>Answer {answer_id}</p>",
        "created_time": 1_700_000_000 + answer_id,
        "updated_time": 1_710_000_000 + answer_id,
        "voteup_count": answer_id,
        "comment_count": 0,
        "author": {
            "id": f"{AUTHOR_TOKEN}-id",
            "name": AUTHOR_NAME,
            "url_token": AUTHOR_TOKEN,
        },
        "question": {"id": 100_000 + answer_id, "title": f"Question {answer_id}"},
    }


@dataclass
class FakeZhihuProvider:
    """Serve one newest-first author-answer page for the real Zhihu collector."""

    answer_ids: list[int] = field(default_factory=lambda: [2, 1])
    content_overrides: dict[int, str] = field(default_factory=dict)
    failure: Exception | None = None
    author_urls: list[str] = field(default_factory=list)
    latest_only_runs: list[bool] = field(default_factory=list)

    def fetch_page(self, _url: str) -> object:
        if self.failure is not None:
            raise self.failure
        answers = [
            _answer_payload(answer_id, self.content_overrides.get(answer_id))
            for answer_id in self.answer_ids
        ]
        return {"data": answers, "paging": {"totals": len(answers), "is_end": True}}

    def sync(self, state, config, should_stop, local_store_root, *, author_url="", latest_only=False):
        self.author_urls.append(author_url)
        self.latest_only_runs.append(latest_only)
        return run_zhihu_history_sync(
            state,
            config,
            should_stop,
            local_store_root,
            author_url=author_url,
            latest_only=latest_only,
            fetch_page=self.fetch_page,
            account_payload={
                "id": "fixture-account-id",
                "url_token": "fixture-account",
                "name": "Fixture Account",
            },
        )


@dataclass(frozen=True)
class ZhihuUpdateServer:
    base_url: str
    root: Path
    provider: FakeZhihuProvider
    detail_path: str

    @property
    def detail_url(self) -> str:
        return self.base_url + self.detail_path

    def cached_answers(self) -> int:
        return ZhihuHistoryStore(zhihu_history_path(self.root)).cached_answers


@pytest.fixture
def zhihu_update_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ZhihuUpdateServer]:
    """Publish a synthetic answerer whose provider pages are injected."""
    from app.web.app import create_app

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("The Zhihu update E2E must not launch a browser")

    monkeypatch.setattr("app.core.zhihu_history.sync_playwright_or_error", forbidden)
    monkeypatch.setattr("app.core.zhihu_history.launch_chromium_context", forbidden)
    monkeypatch.setattr("app.core.zhihu_history.goto_with_retry", forbidden)

    root = tmp_path / "local-store"
    profile = normalize_zhihu_profile_url(PROFILE_URL)
    store = ZhihuHistoryStore(zhihu_history_path(root))
    store.merge_answers(
        tuple(normalize_zhihu_answer_payload(_answer_payload(index), profile) for index in (1, 2)),
        "2026-09-11T00:00:00Z",
    )
    store.save()
    session = query_chat_history(root, source="zhihu", session_view=True).sessions[0]

    provider = FakeZhihuProvider()
    application = create_app(
        root,
        computer_use_settings_path=tmp_path / "settings" / "computer-use-agent.json",
        computer_use_runtime_root=tmp_path / "computer-use-runtime",
    )
    application.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    assert server.server_port != 8666
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch("app.core.zhihu_history_service.sync_zhihu_history", side_effect=provider.sync):
            yield ZhihuUpdateServer(
                base_url=f"http://127.0.0.1:{server.server_port}",
                root=root,
                provider=provider,
                detail_path=(
                    "/browser?view=text&source=zhihu&session_view=1"
                    f"&session={session.stable_id}"
                ),
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _open_answerer(browser: Browser, url: str, width: int = 1138) -> Page:
    context = browser.new_context(
        viewport={"width": width, "height": 959},
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded")
    return page


def _expand_actions(page: Page):
    page.locator(".browser-session-full-export-button").focus()
    button = page.locator("[data-browser-zhihu-answerer-refresh]")
    expect(button).to_have_css("opacity", "1")
    return button


def _banner_text(page: Page) -> tuple[str, str]:
    banner = page.locator("[data-zhihu-answerer-refresh-banner]")
    expect(banner).to_be_visible(timeout=20_000)
    return (
        banner.locator("[data-zhihu-answerer-refresh-title]").inner_text(),
        banner.locator("[data-zhihu-answerer-refresh-copy]").inner_text(),
    )


def test_answerer_update_pulls_new_answers_and_reports_the_result(
    disposable_browser: Browser, zhihu_update_server: ZhihuUpdateServer,
) -> None:
    zhihu_update_server.provider.answer_ids = [3, 2, 1]
    page = _open_answerer(disposable_browser, zhihu_update_server.detail_url)
    try:
        button = _expand_actions(page)
        expect(button).to_have_attribute("aria-label", UPDATE_LABEL)
        assert zhihu_update_server.cached_answers() == 2

        button.click()
        expect(page.locator("#cache_wait_modal")).to_be_visible()
        expect(page.locator("#cache_wait_modal_title")).to_have_text("Updating Zhihu answers")

        title, copy = _banner_text(page)
        assert title == "Added 1 answer"
        assert copy == (
            "Pulled 1 answer from Zhihu answerer Fixture Author and added it to the local text "
            "cache. Checked the newest 3 answers; 2 were already cached."
        )
        assert zhihu_update_server.provider.author_urls == [PROFILE_URL]
        assert zhihu_update_server.provider.latest_only_runs == [True]
        assert zhihu_update_server.cached_answers() == 3
        assert "answerer_added" not in page.url
        expect(page.locator('[aria-label="Zhihu answerer totals"]')).to_contain_text("3")
        expect(page.locator("[data-browser-session-message-source]").filter(has_text="Answer 3")).to_have_count(1)
        expect(page.locator("#cache_wait_modal")).to_be_hidden()

        page.reload(wait_until="domcontentloaded")
        expect(page.locator("[data-zhihu-answerer-refresh-banner]")).to_be_hidden()
    finally:
        page.context.close()


def test_answerer_update_reports_when_nothing_new_was_found(
    disposable_browser: Browser, zhihu_update_server: ZhihuUpdateServer,
) -> None:
    page = _open_answerer(disposable_browser, zhihu_update_server.detail_url)
    try:
        _expand_actions(page).click()

        title, copy = _banner_text(page)
        assert title == "No new answers found"
        assert copy == (
            "No answers newer than the local text cache were found for Zhihu answerer Fixture "
            "Author. Checked the newest 2 answers; all were already cached."
        )
        assert zhihu_update_server.cached_answers() == 2
    finally:
        page.context.close()


def test_answerer_update_counts_re_served_answers_as_refreshed_not_new(
    disposable_browser: Browser, zhihu_update_server: ZhihuUpdateServer,
) -> None:
    zhihu_update_server.provider.content_overrides = {2: "<p>Answer 2 served again</p>"}
    page = _open_answerer(disposable_browser, zhihu_update_server.detail_url)
    try:
        _expand_actions(page).click()

        title, copy = _banner_text(page)
        assert title == "No new answers found"
        assert copy == (
            "No answers newer than the local text cache were found for Zhihu answerer Fixture "
            "Author. Checked the newest 2 answers; all were already cached (1 refreshed)."
        )
        assert zhihu_update_server.cached_answers() == 2
        expect(
            page.locator("[data-browser-session-message-source]").filter(has_text="served again")
        ).to_have_count(1)
    finally:
        page.context.close()


def test_answerer_update_surfaces_a_provider_failure_and_releases_the_action(
    disposable_browser: Browser, zhihu_update_server: ZhihuUpdateServer,
) -> None:
    message = "Zhihu requires human verification. Complete it in Edge, then retry."
    zhihu_update_server.provider.failure = ZhihuVerificationRequiredError(message)
    page = _open_answerer(disposable_browser, zhihu_update_server.detail_url)
    dialogs: list[str] = []

    def accept(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        dialog.accept()

    page.on("dialog", accept)
    try:
        button = _expand_actions(page)
        button.click()

        expect(button).to_be_enabled(timeout=20_000)
        assert dialogs == [message]
        expect(button).not_to_have_attribute("aria-busy", "true")
        expect(button).to_have_attribute("aria-label", UPDATE_LABEL)
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        expect(page.locator("[data-zhihu-answerer-refresh-banner]")).to_be_hidden()
        assert zhihu_update_server.cached_answers() == 2
    finally:
        page.context.close()


@pytest.mark.parametrize("width", [1138, 1024, 390])
def test_expanded_answerer_actions_keep_the_update_button_in_view(
    disposable_browser: Browser, zhihu_update_server: ZhihuUpdateServer, width: int,
) -> None:
    page = _open_answerer(disposable_browser, zhihu_update_server.detail_url, width)
    try:
        _expand_actions(page)
        layout = page.locator("[data-browser-session-actions]").evaluate(
            """root => {
                const x = selector => root.querySelector(selector).getBoundingClientRect();
                const names = [
                    '.browser-session-full-export-button',
                    '.browser-session-refresh-button',
                    '.browser-session-open-original-button',
                    '.browser-session-source-update-button',
                ];
                return {
                    lefts: names.map(name => x(name).left),
                    right: x('.browser-session-source-update-button').right,
                    opacities: names.map(name => getComputedStyle(root.querySelector(name)).opacity),
                    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                };
            }"""
        )
        assert layout["opacities"] == ["1", "1", "1", "1"]
        assert layout["lefts"] == sorted(layout["lefts"])
        assert len(set(layout["lefts"])) == 4
        assert layout["right"] <= width
        assert layout["overflow"] <= 0
    finally:
        page.context.close()
