"""Dataset exchange controls preserve download and preview-before-import boundaries.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

import json
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from flask import Response, request
from playwright.sync_api import expect
from werkzeug.serving import make_server

from tests import test_sidebar_e2e, test_wechat_session_csv
from tests.test_wechat_resource_timeline import _session


disposable_browser = test_sidebar_e2e.disposable_browser
session_app = test_wechat_session_csv.session_app


@pytest.fixture
def exchange_server(session_app):
    app, root = session_app
    app.config["TEST_EXCHANGE_DOWNLOADS"] = {}
    app.config["TEST_EXCHANGE_SYNC_DOWNLOADS"] = []

    @app.before_request
    def synthetic_export_download():
        # Browser-managed downloads bypass renderer routing, so serve exact test bytes.
        prefix = "/browser/wechat/exchange/download/"
        if request.path.startswith(prefix):
            name = app.config["TEST_EXCHANGE_DOWNLOADS"].get(
                request.path[len(prefix) :]
            )
            if name:
                return Response(
                    b"synthetic dataset",
                    headers={
                        "Content-Type": "application/vnd.agenticcontext.wechat-exchange",
                        "Content-Disposition": f'attachment; filename="{name}"',
                    },
                )
        if request.path.startswith(
            "/browser/wechat/session/"
        ) and request.path.endswith("/exchange"):
            app.config["TEST_EXCHANGE_SYNC_DOWNLOADS"].append(request.path)
            return Response(
                "Synchronous dataset export is disabled in this UI fixture.", status=400
            )
        return None

    server = make_server("127.0.0.1", 0, app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = _session(root, kind="group")
    query = (
        f"?view=text&source=wechat&session={session['id']}"
        f"&account={session['account_key']}&chat_kind=group"
    )
    try:
        yield f"http://127.0.0.1:{server.server_port}/browser{query}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _preview(**overrides):
    data = {
        "token": "validated-test-token",
        "package_id": "test-package",
        "format_version": 1,
        "counts": {
            "accounts": 2,
            "sources": 3,
            "conversations": 1,
            "messages": 1234,
            "resources": 100,
        },
        "accounts": [
            {"label": "Same name", "external_id": "account-1"},
            {
                "label": '<img src=x onerror="window.injected=true">',
                "external_id": "account-2",
            },
        ],
        "status": "ready",
        "warnings": ["Source clocks differ."],
        "added_messages": 1230,
        "duplicate_messages": 3,
        "conflicting_messages": 1,
    }
    return {**data, **overrides}


def _open_page(disposable_browser, url, *, width=1280, touch=False):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900},
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.goto(url, wait_until="networkidle")
    return context, page


def _open_import_index(page):
    destination = urlsplit(page.url)
    query = parse_qs(destination.query)
    for key in ("session", "session_page", "page"):
        query.pop(key, None)
    index_url = destination._replace(
        query=urlencode(query, doseq=True), fragment=""
    ).geturl()
    if page.url != index_url:
        page.goto(index_url, wait_until="networkidle")
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")


def _show_import(page):
    _open_import_index(page)
    page.get_by_role("button", name="Import WeChat dataset", exact=True).click()
    dialog = page.get_by_role("dialog", name="Import WeChat dataset", exact=True)
    expect(dialog).to_be_visible()
    return dialog


def _choose_file(page, name="friend.wechatx"):
    page.locator("[data-exchange-file]").set_input_files(
        {
            "name": name,
            "mimeType": "application/octet-stream",
            "buffer": b"synthetic-test-dataset",
        }
    )


@pytest.mark.parametrize("width,touch", [(1280, False), (390, True)])
def test_export_drawer_preserves_csv_default_and_keyboard_navigation(
    disposable_browser, exchange_server, width, touch
):
    context, page = _open_page(
        disposable_browser, exchange_server, width=width, touch=touch
    )
    try:
        toggle = page.locator("#sidebar_toggle")
        if width < 901 and toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        trigger = page.get_by_role(
            "link", name="Export conversation as CSV", exact=True
        )
        menu = page.get_by_role("menu", name="Export conversation", exact=True)
        if touch:
            trigger.tap()
        else:
            trigger.hover()
        expect(menu).to_be_visible()
        csv = menu.get_by_role("menuitem", name="CSV (traditional)", exact=False)
        dataset = menu.get_by_role(
            "menuitem", name="Dataset exchange (.wechatx)", exact=False
        )
        expect(csv).to_have_attribute("href", trigger.get_attribute("href"))
        assert csv.get_attribute("download") == ""
        assert "/exchange?" in dataset.get_attribute("href")
        assert "chat_kind=group" in dataset.get_attribute("href")
        trigger.focus()
        trigger.press("ArrowDown")
        expect(csv).to_be_focused()
        csv.press("ArrowDown")
        expect(dataset).to_be_focused()
        dataset.press("Escape")
        expect(menu).to_be_hidden()
        expect(trigger).to_be_focused()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if not touch:
            with page.expect_download() as download:
                trigger.click()
            assert download.value.suggested_filename.endswith(".csv")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 842, 390, 320])
def test_import_uses_only_the_conversation_index_toolbar(
    disposable_browser, exchange_server, width
):
    """Expose import beside search only in a selected account's conversation index."""
    context, page = _open_page(disposable_browser, exchange_server, width=width)
    try:
        expect(page.locator("[data-wechat-exchange-import-trigger]")).to_have_count(0)
        expect(
            page.locator("#browser_filter_form [data-wechat-exchange-import-trigger]")
        ).to_have_count(0)
        _open_import_index(page)
        trigger = page.get_by_role("button", name="Import WeChat dataset", exact=True)
        expect(trigger).to_be_visible()
        expect(page.locator("[data-wechat-exchange-import-trigger]")).to_have_count(1)
        expect(
            page.locator(
                ".browser-content-toolbar [data-wechat-exchange-import-trigger]"
            )
        ).to_have_count(1)
        expect(
            page.locator("#browser_filter_form [data-wechat-exchange-import-trigger]")
        ).to_have_count(0)
        toggle = page.locator("#sidebar_toggle")
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        source = page.locator(".browser-source-filter-combobox")
        source_geometry = source.evaluate("""node => {
            const box = node.getBoundingClientRect();
            const parent = node.parentElement.getBoundingClientRect();
            return {field: node.parentElement.matches('label.browser-filter-field'),
                width: box.width, fieldWidth: parent.width};
        }""")
        assert source_geometry["field"]
        assert source_geometry["width"] > 0
        assert source_geometry["width"] == pytest.approx(
            source_geometry["fieldWidth"], abs=1
        )
        if width <= 900:
            expect(trigger).to_be_hidden()
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        geometry = trigger.evaluate("""async node => {
            await document.fonts.ready;
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const bounds = element => {
                const box = element.getBoundingClientRect();
                return {left: box.left, right: box.right, width: box.width,
                    height: box.height, x: box.left + box.width / 2,
                    y: box.top + box.height / 2};
            };
            return {button: bounds(node), icon: bounds(node.querySelector('.icon')),
                toolbar: bounds(node.closest('.browser-content-toolbar')),
                search: bounds(document.querySelector('.browser-search-control')),
                mask: getComputedStyle(node.querySelector('.icon')).maskImage,
                overflow: document.documentElement.scrollWidth - innerWidth};
        }""")
        assert [geometry["button"]["width"], geometry["button"]["height"]] == [32, 32]
        assert [geometry["icon"]["width"], geometry["icon"]["height"]] == [18, 18]
        assert "/square.and.arrow.down.svg" in geometry["mask"]
        assert geometry["button"]["y"] == pytest.approx(geometry["toolbar"]["y"], abs=1)
        assert geometry["button"]["right"] <= geometry["search"]["left"] - 8
        assert geometry["overflow"] <= 0
        page.locator("#browser_search_input").focus()
        page.keyboard.press("Shift+Tab")
        expect(trigger).to_be_focused()
        trigger.press("Enter")
        dialog = page.get_by_role("dialog", name="Import WeChat dataset", exact=True)
        expect(dialog).to_be_visible()
        expect(
            dialog.get_by_role("button", name="Choose dataset", exact=True)
        ).to_be_focused()
        dialog.press("Escape")
        expect(dialog).to_be_hidden()
        expect(trigger).to_be_focused()
        destination = urlsplit(page.url)
        query = parse_qs(destination.query)
        query.pop("account", None)
        page.goto(
            destination._replace(query=urlencode(query, doseq=True)).geturl(),
            wait_until="networkidle",
        )
        expect(page.locator("[data-wechat-exchange-import-trigger]")).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_import_requires_validated_preview_and_explicit_commit(
    disposable_browser, exchange_server, width
):
    context, page = _open_page(disposable_browser, exchange_server, width=width)
    requests = []

    def respond(route):
        requests.append((route.request.url, route.request.post_data))
        if route.request.url.endswith("/preview"):
            route.fulfill(json=_preview())
        else:
            route.fulfill(
                json={
                    "status": "imported",
                    "added_messages": 1230,
                    "duplicate_messages": 3,
                    "conflicting_messages": 1,
                    "redirect_url": "https://example.invalid/unsafe",
                }
            )

    page.route("**/api/browser/wechat/exchange/**", respond)
    try:
        dialog = _show_import(page)
        trigger = page.get_by_role("button", name="Import WeChat dataset", exact=True)
        geometry = trigger.evaluate("""node => {
            const icon = node.querySelector('.icon');
            const bounds = node.getBoundingClientRect();
            const glyph = icon.getBoundingClientRect();
            return {button: [bounds.width, bounds.height], icon: [glyph.width, glyph.height], mask: getComputedStyle(icon).maskImage};
        }""")
        assert geometry["button"] == [32, 32]
        assert geometry["icon"] == [18, 18]
        assert "/square.and.arrow.down.svg" in geometry["mask"]
        commit = dialog.get_by_role("button", name="Import", exact=True)
        expect(commit).to_be_disabled()
        _choose_file(page)
        expect(commit).to_be_enabled()
        assert len(requests) == 1
        expect(dialog.locator('[data-exchange-count="messages"]')).to_have_text("1,234")
        expect(dialog.locator("[data-exchange-accounts] li")).to_have_count(2)
        expect(dialog.locator("[data-exchange-accounts]")).to_contain_text("<img src=x")
        expect(dialog.locator("[data-exchange-accounts] img")).to_have_count(0)
        expect(dialog.locator("[data-exchange-conflict-note]")).to_be_visible()
        assert page.evaluate("window.injected") is None
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        commit.click()
        expect(dialog.locator("[data-exchange-status]")).to_contain_text(
            "Imported 1,230 new messages"
        )
        assert len(requests) == 2
        assert json.loads(requests[1][1]) == {"token": "validated-test-token"}
        expect(dialog.locator("[data-exchange-view-result]")).to_be_hidden()
        expect(commit).to_be_hidden()
        dialog.get_by_role("button", name="Close", exact=True).click()
        expect(dialog).to_be_hidden()
        expect(trigger).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize(
    "response,status_code,expected",
    [
        (_preview(status="duplicate", token=""), 200, "already imported"),
        ({"error": "Dataset checksum mismatch."}, 400, "checksum mismatch"),
        (_preview(counts={"messages": 1}), 200, "preview is incomplete"),
    ],
)
def test_invalid_or_duplicate_previews_never_enable_import(
    disposable_browser, exchange_server, response, status_code, expected
):
    context, page = _open_page(disposable_browser, exchange_server)
    requests = []

    def respond(route):
        requests.append(route.request.url)
        route.fulfill(status=status_code, json=response)

    page.route("**/api/browser/wechat/exchange/**", respond)
    try:
        dialog = _show_import(page)
        _choose_file(page)
        expect(dialog.locator("[data-exchange-status]")).to_contain_text(expected)
        expect(dialog.get_by_role("button", name="Import", exact=True)).to_be_disabled()
        assert requests[0].endswith("/preview")
        assert all(url.endswith(("/preview", "/cancel")) for url in requests)
        dialog.press("Escape")
        expect(dialog).to_be_hidden()
    finally:
        context.close()


def test_other_file_types_do_not_reach_the_dataset_service(
    disposable_browser, exchange_server
):
    context, page = _open_page(disposable_browser, exchange_server)
    requests = []
    page.on(
        "request",
        lambda request: (
            requests.append(request.url) if "/exchange/" in request.url else None
        ),
    )
    try:
        dialog = _show_import(page)
        _choose_file(page, "not-a-dataset.ipa")
        expect(dialog.locator("[data-exchange-status]")).to_contain_text(
            "Choose a .wechatx dataset"
        )
        assert not requests
        expect(dialog.get_by_role("button", name="Import", exact=True)).to_be_disabled()
    finally:
        context.close()


@pytest.mark.parametrize("late_response", [False, True])
def test_dismissal_releases_both_ready_and_late_preview_tokens(
    disposable_browser, exchange_server, late_response
):
    context, page = _open_page(disposable_browser, exchange_server)
    pending = []
    cancellations = []

    def respond(route):
        if route.request.url.endswith("/preview"):
            if late_response:
                pending.append(route)
            else:
                route.fulfill(json=_preview())
        else:
            assert route.request.url.endswith("/cancel")
            cancellations.append(json.loads(route.request.post_data))
            route.fulfill(json={"status": "cancelled"})

    page.route("**/api/browser/wechat/exchange/**", respond)
    try:
        dialog = _show_import(page)
        with page.expect_request("**/api/browser/wechat/exchange/preview"):
            _choose_file(page)
        if not late_response:
            expect(
                dialog.get_by_role("button", name="Import", exact=True)
            ).to_be_enabled()
        with page.expect_response(
            "**/api/browser/wechat/exchange/cancel"
        ) as cancellation:
            dialog.press("Escape")
            expect(dialog).to_be_hidden()
            if late_response:
                assert len(pending) == 1
                pending[0].fulfill(json=_preview())
        assert cancellation.value.ok
        assert cancellations == [{"token": "validated-test-token"}]
        dialog = _show_import(page)
        expect(dialog.locator("[data-exchange-preview]")).to_be_hidden()
        expect(dialog.get_by_role("button", name="Import", exact=True)).to_be_disabled()
    finally:
        context.close()


@pytest.mark.parametrize("width", [1023, 677, 390])
def test_share_uses_theme_column_and_toolbar_row_without_covering_search(
    disposable_browser, exchange_server, width
):
    context, page = _open_page(disposable_browser, exchange_server, width=width)
    try:
        toggle = page.locator("#sidebar_toggle")
        if width <= 900 and toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        geometry = page.evaluate("""async () => {
            await document.fonts.ready;
            await new Promise(requestAnimationFrame);
            await new Promise(requestAnimationFrame);
            const bounds = selector => {
                const rect = document.querySelector(selector).getBoundingClientRect();
                return {left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
                    width: rect.width, height: rect.height, x: rect.left + rect.width / 2,
                    y: rect.top + rect.height / 2};
            };
            return {share: bounds('.wechat-conversation-share'), theme: bounds('#global_theme_toggle'),
                row: bounds('.browser-session-detail-actions.browser-local-resources-header-actions'),
                search: bounds('.browser-search-control'), overflow: document.documentElement.scrollWidth - innerWidth};
        }""")
        assert geometry["share"]["width"] == geometry["share"]["height"] == 32
        assert geometry["share"]["x"] == pytest.approx(geometry["theme"]["x"], abs=1)
        assert geometry["share"]["y"] == pytest.approx(geometry["row"]["y"], abs=1)
        assert geometry["search"]["right"] <= geometry["share"]["left"] - 8
        assert geometry["overflow"] <= 0
        trigger = page.get_by_role(
            "link", name="Export conversation as CSV", exact=True
        )
        trigger.hover()
        menu = page.get_by_role("menu", name="Export conversation", exact=True)
        expect(menu).to_be_visible()
        placement = menu.evaluate("""node => {
            const menu = node.getBoundingClientRect();
            const share = document.querySelector('.wechat-conversation-share').getBoundingClientRect();
            const choice = node.querySelector('[role="menuitem"]');
            const rect = choice.getBoundingClientRect();
            return {right: menu.right, shareRight: share.right, top: menu.top,
                shareBottom: share.bottom, left: menu.left,
                hit: choice.contains(document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2))};
        }""")
        assert placement["right"] == pytest.approx(placement["shareRight"], abs=1)
        assert placement["top"] >= placement["shareBottom"]
        assert placement["left"] >= 0 and placement["hit"]
    finally:
        context.close()


@pytest.mark.parametrize("width,touch", [(1280, False), (390, True)])
def test_dataset_export_prepares_asynchronously_and_downloads_without_repeating_job(
    disposable_browser, exchange_server, session_app, width, touch
):
    context, page = _open_page(
        disposable_browser, exchange_server, width=width, touch=touch
    )
    posts, pending, sync_downloads = [], [], []
    token = "test-export-token"
    session_app[0].config["TEST_EXCHANGE_DOWNLOADS"][token] = "conversation.wechatx"
    download_path = f"/browser/wechat/exchange/download/{token}"

    def respond(route):
        if route.request.method == "POST":
            posts.append(json.loads(route.request.post_data))
            route.fulfill(json={"status": "preparing", "token": token})
        else:
            pending.append(route)

    page.route("**/api/browser/wechat/exchange/export**", respond)
    page.route(
        "**/browser/wechat/session/*/exchange?*",
        lambda route: (sync_downloads.append(route.request.url), route.abort()),
    )
    try:
        trigger = page.get_by_role(
            "link", name="Export conversation as CSV", exact=True
        )
        if touch:
            toggle = page.locator("#sidebar_toggle")
            if toggle.get_attribute("aria-expanded") == "true":
                toggle.click()
            trigger.tap()
        else:
            trigger.hover()
        menu = page.locator("[data-exchange-export-menu]")
        option = page.locator("[data-exchange-dataset-export]")
        status = page.locator("[data-exchange-export-status]")
        with page.expect_request("**/api/browser/wechat/exchange/export/*"):
            if touch:
                option.tap()
            else:
                option.click()
        expect(status).to_have_text("Preparing dataset…")
        expect(status).to_have_attribute("aria-live", "polite")
        expect(option).to_have_attribute("aria-disabled", "true")
        page.locator("#browser_search_input").focus()
        page.mouse.move(0, 899)
        page.clock.install()
        page.clock.fast_forward(200)
        expect(menu).to_be_visible()
        option.dispatch_event("click")
        query = parse_qs(urlsplit(exchange_server).query)
        assert posts == [
            {
                "session_id": query["session"][0],
                "account": query["account"][0],
                "chat_kind": query["chat_kind"][0],
            }
        ]
        assert len(pending) == 1
        with page.expect_download() as download:
            pending[0].fulfill(json={"status": "ready", "download_url": download_path})
        assert download.value.suggested_filename == "conversation.wechatx"
        assert download.value.path().read_bytes() == b"synthetic dataset"
        expect(status).to_have_text("Ready. Download started.")
        expect(option).not_to_have_attribute("aria-disabled", "true")
        assert not sync_downloads
        assert not session_app[0].config["TEST_EXCHANGE_SYNC_DOWNLOADS"]
        with page.expect_download() as csv:
            if touch:
                menu.get_by_role(
                    "menuitem", name="CSV (traditional)", exact=False
                ).tap()
            else:
                trigger.click()
        assert csv.value.suggested_filename.endswith(".csv")
    finally:
        context.close()


def test_dataset_export_polls_preparing_until_ready(
    disposable_browser, exchange_server, session_app
):
    context, page = _open_page(disposable_browser, exchange_server)
    polls = []
    token = "poll-export-token"
    session_app[0].config["TEST_EXCHANGE_DOWNLOADS"][token] = "dataset.wechatx"
    download_path = f"/browser/wechat/exchange/download/{token}"

    def respond(route):
        if route.request.method == "POST":
            route.fulfill(json={"status": "preparing", "token": token})
        else:
            polls.append(route.request.url)
            route.fulfill(
                json={"status": "preparing"}
                if len(polls) == 1
                else {
                    "status": "ready",
                    "download_url": download_path,
                }
            )

    page.route("**/api/browser/wechat/exchange/export**", respond)
    try:
        page.locator("[data-exchange-export-trigger]").hover()
        with page.expect_download() as download:
            page.locator("[data-exchange-dataset-export]").click()
        assert download.value.suggested_filename == "dataset.wechatx"
        assert download.value.path().read_bytes() == b"synthetic dataset"
        assert len(polls) == 2
        expect(page.locator("[data-exchange-export-status]")).to_have_text(
            "Ready. Download started."
        )
    finally:
        context.close()


@pytest.mark.parametrize(
    "download_url",
    [
        "https://untrusted.example/package.wechatx",
        "/browser?source=wechat",
        "/browser/wechat/exchange/download/another-token",
        "javascript:alert(1)",
    ],
)
def test_dataset_export_rejects_untrusted_download_addresses(
    disposable_browser, exchange_server, download_url
):
    context, page = _open_page(disposable_browser, exchange_server)
    downloads = []
    page.on("download", lambda download: downloads.append(download))

    def respond(route):
        route.fulfill(
            json={"status": "preparing", "token": "address-test-token"}
            if route.request.method == "POST"
            else {"status": "ready", "download_url": download_url}
        )

    page.route("**/api/browser/wechat/exchange/export**", respond)
    try:
        page.locator("[data-exchange-export-trigger]").hover()
        page.locator("[data-exchange-dataset-export]").click()
        expect(page.locator("[data-exchange-export-status]")).to_contain_text(
            "invalid download address"
        )
        expect(page.locator("[data-exchange-dataset-export]")).not_to_have_attribute(
            "aria-disabled", "true"
        )
        assert not downloads
        assert page.url == exchange_server
    finally:
        context.close()


@pytest.mark.parametrize(
    "failed_response,http_status",
    [
        ({"status": "failed", "error": "<img src=x onerror=alert(1)>"}, 200),
        ({"error": "The export worker is busy."}, 503),
        ({"error": "The export expired."}, 404),
        ({"status": "unknown"}, 200),
    ],
)
def test_dataset_export_reports_failures_as_text_and_restores_choice(
    disposable_browser, exchange_server, failed_response, http_status
):
    context, page = _open_page(disposable_browser, exchange_server)

    def respond(route):
        if route.request.method == "POST":
            route.fulfill(json={"status": "preparing", "token": "failed-export-token"})
        else:
            route.fulfill(status=http_status, json=failed_response)

    page.route("**/api/browser/wechat/exchange/export**", respond)
    try:
        page.locator("[data-exchange-export-trigger]").hover()
        option = page.locator("[data-exchange-dataset-export]")
        option.click()
        status = page.locator("[data-exchange-export-status]")
        expect(status).to_contain_text("Choose Dataset exchange again")
        expect(status.locator("img")).to_have_count(0)
        if "error" in failed_response:
            expect(status).to_contain_text(failed_response["error"])
        expect(status).to_contain_text(
            "again to resume" if http_status == 503 else "again to retry"
        )
        expect(option).not_to_have_attribute("aria-disabled", "true")
    finally:
        context.close()


def test_interrupted_export_status_can_resume_without_creating_a_second_job(
    disposable_browser, exchange_server, session_app
):
    context, page = _open_page(disposable_browser, exchange_server)
    posts, polls = [], []
    token = "resume-export-token"
    session_app[0].config["TEST_EXCHANGE_DOWNLOADS"][token] = "dataset.wechatx"
    download_path = f"/browser/wechat/exchange/download/{token}"

    def respond(route):
        if route.request.method == "POST":
            posts.append(route.request.url)
            route.fulfill(json={"status": "preparing", "token": token})
        else:
            polls.append(route.request.url)
            if len(polls) == 1:
                route.abort("failed")
            else:
                route.fulfill(json={"status": "ready", "download_url": download_path})

    page.route("**/api/browser/wechat/exchange/export**", respond)
    try:
        page.locator("[data-exchange-export-trigger]").hover()
        option = page.locator("[data-exchange-dataset-export]")
        option.click()
        expect(page.locator("[data-exchange-export-status]")).to_contain_text(
            "again to resume"
        )
        with page.expect_download() as download:
            option.click()
        assert download.value.suggested_filename == "dataset.wechatx"
        assert download.value.path().read_bytes() == b"synthetic dataset"
        assert len(posts) == 1 and len(polls) == 2
        expect(page.locator("[data-exchange-export-status]")).to_have_text(
            "Ready. Download started."
        )
    finally:
        context.close()


def test_async_dataset_download_uses_real_backend_and_valid_codec_package(
    disposable_browser, exchange_server, session_app
):
    from app.core.wechat_exchange import read_package

    context, page = _open_page(disposable_browser, exchange_server)
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    try:
        page.locator("[data-exchange-export-trigger]").hover()
        with page.expect_download() as download:
            page.locator("[data-exchange-dataset-export]").click()
        assert download.value.suggested_filename.endswith(".wechatx")
        path = download.value.path()
        assert path.read_bytes().startswith(b"PK")
        verified = read_package(path)
        query = parse_qs(urlsplit(exchange_server).query)
        assert verified.manifest["scope"]["root_session_id"] == query["session"][0]
        assert verified.manifest["counts"]["messages"] >= 215
        assert list(verified.iter_rows("exchange.timeline"))
        assert any(
            url.endswith("/api/browser/wechat/exchange/export") for url in requests
        )
        assert not session_app[0].config["TEST_EXCHANGE_DOWNLOADS"]
        assert not session_app[0].config["TEST_EXCHANGE_SYNC_DOWNLOADS"]
        expect(page.locator("[data-exchange-export-status]")).to_have_text(
            "Ready. Download started."
        )
        assert page.url == exchange_server
    finally:
        context.close()
