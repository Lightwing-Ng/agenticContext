"""WeChat refresh actions retain shared geometry, scope, and honest progress.

Code version: v1.5.1-codex.0
"""

from __future__ import annotations

from threading import Thread
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from tests import test_wechat_browser_e2e as wechat_fixtures
from tests import test_wechat_group_roster_routes as roster_fixtures


disposable_browser = wechat_fixtures.disposable_browser
wechat_server = wechat_fixtures.wechat_server
roster_app = roster_fixtures.roster_app
_STATUS_URL = "/api/browser/wechat/refresh/synthetic-refresh-status-identifier"


@pytest.fixture
def refresh_keys(monkeypatch):
    """Expose only synthetic availability; no test reads a real database key."""
    from app.core.wechat_mac_refresh import WechatMacRefreshService

    state = {"available": True}
    monkeypatch.setattr(
        WechatMacRefreshService,
        "available",
        lambda *_args, **_kwargs: state["available"],
    )
    return state


@pytest.fixture
def roster_server(roster_app):
    server = make_server("127.0.0.1", 0, roster_app.app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        roster_app.app.extensions["runtime_shutdown"]()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _open_chat(browser, server, *, width=1280, kind="group", official=False):
    context = browser.new_context(
        viewport={"width": width, "height": 937}, reduced_motion="reduce"
    )
    page = context.new_page()
    account = "account:wxid_account_b" if official else "account:wxid_account_a"
    if kind == "unclassified":
        account = "source:xlsx-3"
    parameters = {
        "view": "text",
        "source": "wechat",
        "account": account,
        "chat_kind": kind,
        "sort": "oldest",
        "session_view": "1",
    }
    page.goto(server + "/browser?" + urlencode(parameters), wait_until="networkidle")
    expect(page.locator("[data-wechat-conversation-refresh]")).to_have_count(0)
    title = "Official fixture" if official else "Same title"
    link = (
        page.locator("tr[data-chat-session-id] a[href*='session=']").first
        if kind == "unclassified"
        else page.get_by_role("link", name=title, exact=True)
    )
    destination = urlsplit(urljoin(server, link.get_attribute("href")))
    values = parse_qs(destination.query)
    values.update(session_page=["7"], page=["2"])
    page.goto(
        destination._replace(query=urlencode(values, doseq=True)).geturl(),
        wait_until="networkidle",
    )
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
    return context, page


@pytest.mark.parametrize(
    ("available", "kind", "official", "expected"),
    [
        (True, "direct", False, True),
        (True, "group", False, True),
        (False, "direct", False, False),
        (False, "group", False, False),
        (True, "direct", True, False),
        (True, "unclassified", False, False),
    ],
)
def test_refresh_is_visible_only_for_configured_private_and_group_chats(
    disposable_browser, wechat_server, refresh_keys, available, kind, official, expected
):
    refresh_keys["available"] = available
    context, page = _open_chat(
        disposable_browser, wechat_server, kind=kind, official=official
    )
    try:
        refresh = page.get_by_role("button", name="Refresh chat history", exact=True)
        expect(refresh).to_have_count(1 if expected else 0)
        if expected:
            expect(refresh).to_be_visible()
        expect(page.get_by_role("link", name="Export conversation as CSV", exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("width", [1680, 1280, 735, 390, 320])
def test_refresh_keeps_shared_geometry_and_does_not_open_the_export_menu(
    disposable_browser, wechat_server, refresh_keys, width
):
    context, page = _open_chat(disposable_browser, wechat_server, width=width)
    try:
        refresh = page.get_by_role("button", name="Refresh chat history", exact=True)
        share = page.get_by_role("link", name="Export conversation as CSV", exact=True)
        menu = page.get_by_role("menu", name="Export conversation", exact=True)
        refresh.hover()
        expect(menu).to_be_hidden()
        refresh.focus()
        expect(refresh).to_be_focused()
        expect(menu).to_be_hidden()
        geometry = page.evaluate("""async () => {
            await document.fonts.ready;
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const bounds = selector => {
                const box = document.querySelector(selector).getBoundingClientRect();
                return {left: box.left, right: box.right, top: box.top,
                    width: box.width, height: box.height,
                    x: box.left + box.width / 2, y: box.top + box.height / 2};
            };
            return {
                refresh: bounds('[data-wechat-conversation-refresh]'),
                glyph: bounds('[data-wechat-conversation-refresh] .wechat-refresh-icon'),
                share: bounds('.wechat-conversation-share'),
                theme: bounds('#global_theme_toggle'),
                search: bounds('.browser-search-control'),
                scopeRemove: bounds('.browser-search-session-tag > button'),
                input: bounds('#browser_search_input'),
                inputPadding: parseFloat(getComputedStyle(document.getElementById('browser_search_input')).paddingLeft)
                    + parseFloat(getComputedStyle(document.getElementById('browser_search_input')).paddingRight),
                gap: parseFloat(getComputedStyle(document.documentElement)
                    .getPropertyValue('--layout-global-action-gap')),
                mask: getComputedStyle(document.querySelector('[data-wechat-conversation-refresh] .wechat-refresh-icon')).maskImage,
                overflow: document.documentElement.scrollWidth - innerWidth,
            };
        }""")
        assert [geometry["refresh"]["width"], geometry["refresh"]["height"]] == [32, 32]
        assert [geometry["glyph"]["width"], geometry["glyph"]["height"]] == [18, 18]
        assert geometry["mask"].endswith('/square.and.arrow.down.on.square.svg")')
        assert geometry["share"]["left"] - geometry["refresh"]["right"] == pytest.approx(
            geometry["gap"], abs=1
        ), geometry
        assert geometry["refresh"]["y"] == pytest.approx(geometry["share"]["y"], abs=1), geometry
        search_cap = geometry["search"]["right"] - geometry["search"]["height"] / 2
        assert search_cap == pytest.approx(geometry["theme"]["x"], abs=1), geometry
        assert geometry["search"]["left"] - geometry["share"]["right"] == pytest.approx(
            geometry["gap"], abs=1
        ), geometry
        assert geometry["scopeRemove"]["left"] >= geometry["search"]["left"], geometry
        assert geometry["scopeRemove"]["right"] <= geometry["input"]["left"], geometry
        assert geometry["input"]["width"] > geometry["inputPadding"], geometry
        assert geometry["input"]["right"] <= geometry["search"]["right"], geometry
        assert geometry["overflow"] <= 1, geometry
        page.keyboard.press("Tab")
        expect(share).to_be_focused()
        if width <= 900:
            page.locator("#sidebar_toggle").click()
            expect(page.locator("#sidebar_toggle")).to_have_attribute("aria-expanded", "true")
            expect(refresh).to_be_hidden()
            expect(share).to_be_hidden()
            expect(page.locator("#global_theme_toggle")).to_be_visible()
            page.locator("#sidebar_toggle").click()
            expect(refresh).to_be_visible()
            expect(share).to_be_visible()
    finally:
        context.close()


def test_refresh_shows_immediate_progress_and_sends_only_one_pending_request(
    disposable_browser, wechat_server, refresh_keys
):
    context, page = _open_chat(disposable_browser, wechat_server)
    pending = []
    try:
        refresh = page.locator("[data-wechat-conversation-refresh]")
        endpoint = refresh.get_attribute("data-wechat-refresh-url")
        session_id = refresh.get_attribute("data-wechat-session-id")
        page.route(urljoin(wechat_server, endpoint), lambda route: pending.append(route))
        refresh.click(no_wait_after=True)
        expect(refresh).to_be_disabled()
        expect(refresh).to_have_attribute("aria-busy", "true")
        modal = page.locator("#cache_wait_modal")
        expect(modal).to_be_visible()
        expect(page.locator("#cache_wait_modal_title")).to_have_text("Refreshing chat history")
        expect(modal.get_by_role("progressbar")).to_have_count(0)
        refresh.evaluate("button => button.dispatchEvent(new MouseEvent('click', {bubbles:true}))")
        assert len(pending) == 1
        request = pending[0].request
        assert request.method == "POST"
        assert request.post_data_json == {"session_id": session_id}
        pending.pop().fulfill(status=503, json={"error": "The test source is unavailable."})
        expect(refresh).to_be_enabled()
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
    finally:
        for route in pending:
            route.abort()
        context.close()


@pytest.mark.parametrize("stage", ["start", "status"])
def test_refresh_failure_restores_the_action_and_shows_a_shared_error_notice(
    disposable_browser, wechat_server, refresh_keys, stage
):
    context, page = _open_chat(disposable_browser, wechat_server)
    try:
        refresh = page.get_by_role("button", name="Refresh chat history", exact=True)
        endpoint = refresh.get_attribute("data-wechat-refresh-url")
        original_url = page.url
        error = "The synthetic source could not be refreshed."
        if stage == "start":
            page.route(urljoin(wechat_server, endpoint), lambda route: route.fulfill(
                status=409, json={"error": error}
            ))
        else:
            page.route(urljoin(wechat_server, endpoint), lambda route: route.fulfill(
                status=202,
                json={"state": "running", "status_url": _STATUS_URL},
            ))
            page.route("**" + _STATUS_URL, lambda route: route.fulfill(
                json={"state": "failed", "message": error}
            ))
        refresh.click()
        banner = page.locator("[data-wechat-refresh-banner]")
        expect(banner).to_be_visible()
        classes = banner.get_attribute("class").split()
        assert "error" in classes and "notice" not in classes
        expect(banner.locator("[data-wechat-refresh-copy]")).to_have_text(error)
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        expect(refresh).to_be_enabled()
        expect(refresh).not_to_have_attribute("aria-busy", "true")
        expect(refresh).to_have_attribute("title", "Refresh chat history")
        assert page.url == original_url
        banner.hover()
        banner.get_by_role("button", name="Close refresh result", exact=True).click()
        expect(banner).to_be_hidden()
    finally:
        context.close()


@pytest.mark.parametrize(
    ("state", "member_snapshot", "title", "snapshot_copy"),
    [
        ("succeeded", "saved", "Chat history refreshed", "Member snapshot saved."),
        ("succeeded", "unavailable", "Chat history refreshed", "Local member list unavailable."),
        ("succeeded", "failed", "Chat history refreshed", "Member snapshot could not be saved."),
        ("failed", "saved", "Unable to refresh chat history", "Member snapshot saved."),
    ],
)
def test_group_refresh_shows_member_snapshot_outcome_after_reload(
    disposable_browser, roster_server, roster_app, refresh_keys,
    state, member_snapshot, title, snapshot_copy,
):
    context = disposable_browser.new_context(reduced_motion="reduce")
    page = context.new_page()
    destination = roster_server + "/browser?" + urlencode({
        "view": "text", "source": "wechat", "account": "account:" + roster_app.owner,
        "chat_kind": "group", "session": roster_app.group, "page": 1,
    })
    status_url = _STATUS_URL
    message_failure = "The test chat history could not be refreshed."
    requests = []
    try:
        page.goto(destination, wait_until="networkidle")
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        refresh = page.get_by_role("button", name="Refresh chat history", exact=True)
        history = page.locator(".wechat-group-membership-history")
        history.locator("summary").first.click()
        expect(history).to_have_attribute("open", "")
        expect(history.get_by_text("Use Refresh chat history to save the local member list.")).to_be_visible()
        expect(page.get_by_role("button", name="Save member snapshot", exact=True)).to_have_count(0)

        def start(route):
            requests.append((route.request.method, route.request.post_data_json))
            route.fulfill(status=202, json={"state": "running", "status_url": status_url})

        def reject_standalone_capture(route):
            requests.append((route.request.method, route.request.url))
            route.abort()

        def finish(route):
            if member_snapshot == "saved":
                roster_app.repository.save(roster_app.state["snapshot"])
            route.fulfill(json={
                "state": state,
                "added": 0,
                "total": 1,
                "member_snapshot": member_snapshot,
                "message": message_failure,
            })

        endpoint = refresh.get_attribute("data-wechat-refresh-url")
        page.route(urljoin(roster_server, endpoint), start)
        page.route("**/browser/wechat/group-roster", reject_standalone_capture)
        page.route("**" + status_url, finish)
        with page.expect_navigation(wait_until="domcontentloaded"):
            refresh.click(no_wait_after=True)

        assert page.url == destination
        assert requests == [("POST", {"session_id": roster_app.group})]
        roster = page.locator("#wechat_group_roster")
        expect(roster).to_have_attribute(
            "data-wechat-group-roster", "ready" if member_snapshot == "saved" else "absent"
        )
        banner = page.locator("[data-wechat-refresh-banner]")
        expect(banner).to_be_visible()
        expect(banner.locator("[data-wechat-refresh-title]")).to_have_text(title)
        message_copy = (
            message_failure if state == "failed"
            else "No new messages found. The conversation contains 1 message."
        )
        expect(banner.locator("[data-wechat-refresh-copy]")).to_have_text(
            f"{message_copy} {snapshot_copy}"
        )
    finally:
        context.close()


@pytest.mark.parametrize(
    ("added", "upgraded", "reparsed", "summary"),
    [
        (1234, 0, 0, "Added 1,234 messages. The conversation now contains 5,678 messages."),
        (1234, 6, 0, "Added 1,234 messages. Restored 6 message placeholders. The conversation now contains 5,678 messages."),
        (0, 0, 4, "Repaired details for 4 messages. The conversation contains 5,678 messages."),
        (1234, 6, 4, "Added 1,234 messages. Restored 6 message placeholders. Repaired details for 4 messages. The conversation now contains 5,678 messages."),
    ],
)
def test_refresh_reloads_the_same_scope_and_preserves_contact_history_for_one_notice(
    disposable_browser, wechat_server, refresh_keys, added, upgraded, reparsed, summary
):
    context, page = _open_chat(disposable_browser, wechat_server, kind="direct")
    try:
        refresh = page.get_by_role("button", name="Refresh chat history", exact=True)
        endpoint = refresh.get_attribute("data-wechat-refresh-url")
        original_url = page.url
        page.evaluate("""() => {
            const port = document.querySelector('[data-chat-scrollport]');
            const message = port.querySelector('[data-chat-message-id][id]');
            window.history.replaceState({
                unrelated: {value: 'preserved'},
                agenticContextWechatContactReturnV1: {
                    version: 1, url: window.location.href,
                    scrollTop: port.scrollTop, scrollLeft: port.scrollLeft,
                    documentX: scrollX, documentY: scrollY,
                    anchorId: message.id,
                    anchorOffset: message.getBoundingClientRect().top - port.getBoundingClientRect().top,
                    openDetails: [], openForwarded: [], focusedLinkId: '',
                },
            }, '', window.location.href);
        }""")
        page.route(urljoin(wechat_server, endpoint), lambda route: route.fulfill(
            status=202,
            json={"state": "running", "status_url": _STATUS_URL},
        ))
        reads = []

        def status(route):
            reads.append(route.request.url)
            route.fulfill(json={
                "state": "running" if len(reads) == 1 else "succeeded",
                "added": added, "upgraded": upgraded, "reparsed": reparsed,
                "total": 5678, "partial": True,
            })

        page.route("**" + _STATUS_URL, status)
        with page.expect_navigation(wait_until="domcontentloaded"):
            refresh.click(no_wait_after=True)
        assert page.url == original_url
        banner = page.locator("[data-wechat-refresh-banner]")
        expect(banner).to_be_visible()
        expect(banner.locator("[data-wechat-refresh-title]")).to_have_text("Chat history partly refreshed")
        expect(banner.locator("[data-wechat-refresh-copy]")).to_have_text(
            summary + " Some sources could not be refreshed."
        )
        state = page.evaluate("window.history.state")
        assert state["unrelated"] == {"value": "preserved"}
        assert state["agenticContextWechatContactReturnV1"]["url"] == original_url
        assert state["agenticContextWechatContactReturnV1"]["anchorId"]
        assert "agenticContextWechatRefreshV1" not in state
        assert len(reads) == 2
        page.reload(wait_until="networkidle")
        expect(page.locator("[data-wechat-refresh-banner]")).to_be_hidden()
        assert page.url == original_url
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390, 320])
@pytest.mark.parametrize("has_changes", [True, False])
def test_refresh_result_places_each_metric_on_a_separate_responsive_line(
    disposable_browser, wechat_server, refresh_keys, width, has_changes
):
    context, page = _open_chat(disposable_browser, wechat_server, width=width)
    writes = []
    page.route("**/api/browser/wechat/refresh", lambda route: writes.append(route.request))
    try:
        lines = (
            [
                "Added 1,234 messages.",
                "Restored 6 message placeholders.",
                "Repaired details for 4 messages.",
                "The conversation now contains 5,678 messages.",
                "Some sources could not be refreshed.",
            ]
            if has_changes
            else ["No new messages found.", "The conversation contains 5,678 messages."]
        )
        page.evaluate("""hasChanges => {
            window.history.replaceState({
                ...window.history.state,
                agenticContextWechatRefreshV1: {
                    version: 1, url: window.location.href,
                    added: hasChanges ? 1234 : 0,
                    upgraded: hasChanges ? 6 : 0,
                    reparsed: hasChanges ? 4 : 0,
                    total: 5678, partial: hasChanges,
                },
            }, '', window.location.href);
        }""", has_changes)
        page.reload(wait_until="networkidle")
        banner = page.locator("[data-wechat-refresh-banner]")
        copy = banner.locator("[data-wechat-refresh-copy]")
        expect(banner).to_be_visible()
        assert copy.text_content() == "\n".join(lines)
        assert copy.inner_text().splitlines() == lines
        geometry = copy.evaluate(r"""async node => {
            await document.fonts.ready;
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const owner = node.getBoundingClientRect();
            const banner = node.closest('[data-wechat-refresh-banner]').getBoundingClientRect();
            let offset = 0;
            const lines = node.textContent.split('\n').map(text => {
                const range = document.createRange();
                range.setStart(node.firstChild, offset);
                range.setEnd(node.firstChild, offset + text.length);
                offset += text.length + 1;
                const rects = Array.from(range.getClientRects()).filter(rect => rect.width > 0);
                return {
                    top: rects[0].top,
                    bottom: rects.at(-1).bottom,
                    left: Math.min(...rects.map(rect => rect.left)),
                    right: Math.max(...rects.map(rect => rect.right)),
                };
            });
            return {
                lines, whiteSpace: getComputedStyle(node).whiteSpace,
                owner: {left: owner.left, right: owner.right, top: owner.top, bottom: owner.bottom},
                banner: {left: banner.left, right: banner.right, top: banner.top, bottom: banner.bottom},
                overflow: document.documentElement.scrollWidth - innerWidth,
            };
        }""")
        assert geometry["whiteSpace"] == "pre-line"
        for previous, current in zip(geometry["lines"], geometry["lines"][1:]):
            assert current["top"] >= previous["bottom"] - 1, geometry
        for line in geometry["lines"]:
            assert line["left"] >= geometry["owner"]["left"] - 1, geometry
            assert line["right"] <= geometry["owner"]["right"] + 1, geometry
            assert line["top"] >= geometry["banner"]["top"] - 1, geometry
            assert line["bottom"] <= geometry["banner"]["bottom"] + 1, geometry
        assert geometry["overflow"] <= 1, geometry
        assert not writes
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_refresh_progress_stays_available_in_cache_tasks_after_dismissal(
    disposable_browser, wechat_server, refresh_keys, width
):
    context, page = _open_chat(disposable_browser, wechat_server, width=width)
    status_url = _STATUS_URL
    snapshot = {
        "state": "running", "phase": "reading", "parts_total": 7,
        "parts_completed": 1, "part_current": 2, "checked": 1234,
        "candidates": 56, "added": 0, "upgraded": 0,
        "database_path": "/private/synthetic-source-never-display.db",
    }
    try:
        refresh = page.locator("[data-wechat-conversation-refresh]")
        page.route(
            urljoin(wechat_server, refresh.get_attribute("data-wechat-refresh-url")),
            lambda route: route.fulfill(
                status=202, json={"state": "running", "status_url": status_url}
            ),
        )
        page.route("**" + status_url, lambda route: route.fulfill(json=dict(snapshot)))
        page.route("**/api/cache/activity", lambda route: route.fulfill(json={"tasks": []}))
        modal = page.locator("#cache_wait_modal")
        copy = page.locator("#cache_wait_modal_copy")
        close = modal.get_by_role("button", name="Close waiting notification")
        trigger = page.locator("[data-cache-activity-trigger]")
        panel = page.locator("[data-cache-activity-panel]")
        refresh.click()
        expect(modal).to_be_visible()
        expect(close).to_be_focused()
        expect(close).to_have_css("opacity", "1")
        reading = "Reading source 2 of 7. 1,234 messages checked. 56 possible updates identified."
        expect(copy).to_have_text(reading)
        assert "synthetic-source" not in modal.inner_text()
        assert "Added" not in modal.inner_text()
        source_meter = modal.get_by_role("progressbar", name="WeChat refresh source processing progress")
        expect(source_meter).to_have_attribute("aria-valuenow", "14")
        expect(source_meter).to_have_attribute("aria-valuetext", "1 / 7 sources processed (14%)")
        geometry = modal.locator(".workspace-modal-dialog").bounding_box()
        assert geometry["width"] == pytest.approx(min(420, width - 32), abs=1)

        snapshot.update(phase="comparing", part_rows_processed=1_234, part_rows_total=2_000)
        record_meter = modal.get_by_role("progressbar", name="Source 2 of 7 record processing progress")
        expect(record_meter).to_have_attribute("aria-valuenow", "61")
        expect(record_meter).to_have_attribute("aria-valuetext", "1,234 / 2,000 records processed (61%)")

        close.click()
        expect(modal).to_be_hidden()
        expect(trigger).to_have_accessible_name("Show cache tasks")
        trigger.click()
        row = panel.locator("[data-cache-activity-task]")
        task = row.get_by_role("button", name="Refreshing chat history", exact=True)
        meter = row.get_by_role("progressbar", name="Refreshing chat history source processing progress")
        expect(row).to_have_count(1)
        expect(task).to_be_visible()
        expect(meter).to_be_visible()
        expect(row).to_contain_text("1 / 7 sources processed")
        expect(row).to_contain_text("Comparing messages in source 2 of 7.")

        snapshot.update(
            phase="checking", parts_completed=7, part_current=0,
            checked=12_345, candidates=64, skipped_parts=1, unavailable_parts=2,
        )
        details = (
            "12,345 messages checked. 64 possible updates identified. "
            "1 source could not be verified. 2 additional sources need verified keys."
        )
        expect(row).to_contain_text("Validating possible changes.")
        expect(row).to_contain_text(details)
        snapshot.update(phase="saving")
        expect(row).to_contain_text("Saving verified changes.")
        expect(modal).to_be_hidden()
        expect(refresh).to_be_disabled()

        if width == 390:
            task.focus()
            task.press("Enter")
        else:
            task.click()
        expect(panel).to_be_hidden()
        expect(modal).to_be_visible()
        expect(close).to_be_focused()
        expect(copy).to_have_text(f"Saving verified changes. {details}")
        expect(source_meter).to_have_attribute("aria-valuenow", "100")
        expect(source_meter).to_have_attribute("aria-valuetext", "7 / 7 sources processed (100%)")
        expect(refresh).to_be_disabled()
        page.keyboard.press("Escape")
        expect(modal).to_be_hidden()
        expect(trigger).to_be_focused()
        expect(refresh).to_be_disabled()

        snapshot.update(state="failed", message="The synthetic task ended safely.")
        expect(refresh).to_be_enabled()
        expect(row).to_have_count(0)
        expect(trigger).to_be_hidden()
        expect(page.locator("[data-wechat-refresh-copy]")).to_have_text(
            "The synthetic task ended safely."
        )
    finally:
        context.close()


def test_waiting_handle_updates_respect_latest_owner_finish_and_dismissal(
    disposable_browser, wechat_server, refresh_keys
):
    context, page = _open_chat(disposable_browser, wechat_server)
    try:
        page.evaluate("""() => {
            window.waitA = CacheWaitModal.show({title: 'First task', copy: 'Initial first copy'});
            window.waitB = CacheWaitModal.show({title: 'Second task', copy: 'Second copy'});
            waitA.update({copy: 'Updated first copy'});
        }""")
        expect(page.locator("#cache_wait_modal_title")).to_have_text("Second task")
        page.evaluate("waitB.finish()")
        expect(page.locator("#cache_wait_modal_copy")).to_have_text("Updated first copy")
        page.locator("#cache_wait_modal .workspace-modal-dialog").hover()
        page.get_by_role("button", name="Close waiting notification").click()
        page.evaluate("waitA.update({copy: 'Late progress'})")
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        page.evaluate("waitA.finish(); waitA.update({copy: 'Finished progress'})")
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
    finally:
        context.close()
