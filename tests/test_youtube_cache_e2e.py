"""Exercise the inline YouTube Cache controls and source switching.

Code version: v1.5.1-codex.0
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, expect

from test_sidebar_e2e import disposable_browser as disposable_browser
from test_sidebar_e2e import macos_sidebar_server_url as macos_sidebar_server_url


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(("width", "height"), ((1280, 900), (390, 844)))
def test_youtube_cache_sidebar_submits_the_keyboard_selected_media_mode(
    disposable_browser: Browser,
    macos_sidebar_server_url: str,
    width: int,
    height: int,
) -> None:
    video_id = "FixtureA123"
    url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}&index=2"
    filename = "YouTube Audio · Live Session – 现场录音 · Studio Sessions.m4a"
    older_name = "YouTube Video · " + "フィールド録音セッション" * 5 + " · Studio Sessions.mp4"
    status_path = "/api/cache/youtube/jobs/test-request"
    media_path = f"/api/cache/youtube/media/{video_id}/audio"
    older_path = "/api/cache/youtube/media/eLDdNjQNSY8/video"
    submitted = []
    status_requests = 0
    progress_snapshot = None
    allow_completion = False
    completed = False
    fail_next = False
    context = disposable_browser.new_context(viewport={"width": width, "height": height})

    def capture_download(route) -> None:
        submitted.append(route.request.post_data_json)
        if fail_next:
            route.fulfill(status=409, json={"error": "A YouTube download is already running."})
        else:
            route.fulfill(status=202, json={"status_url": status_path})

    def download_status(route) -> None:
        nonlocal status_requests, completed
        status_requests += 1
        if not allow_completion:
            data = {"status": "running"}
            if progress_snapshot is not None:
                data["progress"] = progress_snapshot
            route.fulfill(json=data)
            return
        completed = True
        route.fulfill(json={
            "status": "completed",
            "filename": filename,
            "size_bytes": 2048,
            "download_url": media_path,
            "cached": False,
        })

    def recent_downloads(route) -> None:
        items = [{"filename": older_name, "download_url": older_path}]
        if completed:
            items.insert(0, {"filename": filename, "download_url": media_path})
        route.fulfill(json={"items": items})

    context.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "browser": "safari",
        "logged_in": True,
        "can_download": True,
        "account_name": "Signed in",
        "message": "Ready.",
    }))
    context.route("**/api/cache/youtube/download", capture_download)
    context.route("**/api/cache/youtube/recent", recent_downloads)
    context.route(f"**{status_path}", download_status)
    page = context.new_page()
    try:
        media_url = f"{macos_sidebar_server_url}/cache/chatgpt/media/safari"
        page.goto(media_url, wait_until="domcontentloaded")
        toggle = page.locator("#sidebar_toggle")
        if toggle.is_visible() and toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
        sidebar = page.locator("#app_sidebar")
        trigger = sidebar.locator("[data-cache-source-switcher-trigger]")
        youtube = sidebar.get_by_role("option", name="YouTube", exact=True)
        browser_controls = sidebar.locator("[data-browser-session-panel]")
        panel = sidebar.locator("[data-youtube-cache-panel]")
        expect(browser_controls).to_be_visible()
        expect(panel).to_be_hidden()

        trigger.press("End")
        expect(youtube).to_be_focused()
        youtube.press("Enter")

        expect(panel).to_be_visible()
        expect(browser_controls).to_be_hidden()
        expect(page.get_by_role("dialog", name="YouTube cache")).to_have_count(0)
        expect(trigger).to_have_accessible_name("Cache source: YouTube")
        expect(page).to_have_url(media_url)

        recent = panel.get_by_role("region", name="Recent cache")
        older = recent.get_by_role("link", name=older_name)
        expect(older).to_be_visible()
        expect(older).to_have_attribute("title", older_name)
        expect(older.locator("span")).to_have_css("text-overflow", "ellipsis")
        expect(older.locator("span")).to_have_css("white-space", "nowrap")
        assert older.locator("span").evaluate("label => label.scrollWidth > label.clientWidth")
        progress = panel.get_by_role("progressbar", name="YouTube cache progress")
        expect(progress).to_be_hidden()

        url_input = panel.get_by_role("textbox", name="YouTube URL")
        expect(url_input).to_be_focused()
        clear_url = panel.get_by_role("button", name="Clear YouTube URL")
        expect(clear_url).to_be_hidden()
        video = panel.get_by_role("radio", name="Video")
        audio = panel.get_by_role("radio", name="Audio")
        pill = panel.locator(".segmented-control")
        submit = panel.locator('button[type="submit"]')
        expect(video).to_be_checked()
        expect(pill).to_have_attribute("data-segmented-active-index", "0")
        expect(submit).to_have_text("Cache video")
        right_edge_error = """button => Math.abs(
            button.closest('form').getBoundingClientRect().right - button.getBoundingClientRect().right
        )"""
        assert submit.evaluate(right_edge_error) <= 1
        video.press("ArrowRight")
        expect(audio).to_be_checked()
        expect(audio).to_be_focused()
        expect(pill).to_have_attribute("data-segmented-active-index", "1")
        assert pill.evaluate("element => element.scrollWidth <= element.clientWidth + 1")

        url_input.fill(url)
        expect(clear_url).to_have_css("opacity", "1")
        clear_url.click()
        expect(url_input).to_have_value("")
        expect(url_input).to_be_focused()
        expect(clear_url).to_be_hidden()
        assert not submitted

        url_input.fill(url)
        expect(submit).to_have_text("Cache audio")
        assert submit.evaluate(right_edge_error) <= 1
        submit.click()
        status = panel.get_by_role("status")
        expect(status).to_have_text("Preparing YouTube audio…")
        expect(progress).to_be_hidden()
        progress_snapshot = {"phase": "downloading", "stream": "audio", "downloaded_bytes": 512, "total_bytes": None}
        expect(status).to_have_text("Downloading YouTube audio stream… · 512 bytes received")
        expect(progress).to_be_hidden()
        progress_snapshot = {"phase": "downloading", "stream": "audio", "downloaded_bytes": 1024, "total_bytes": 4096}
        expect(progress).to_be_visible()
        expect(progress).to_have_css("height", "6px")
        expect(progress).to_have_attribute("aria-valuenow", "25")
        expect(status).to_have_text("Downloading YouTube audio stream… · 1,024 / 4,096 bytes (25%)")
        fill = progress.locator(".status-progress-fill")
        standard_color = page.locator("#status_progress_fill").evaluate("fill => getComputedStyle(fill).backgroundColor")
        expect(fill).to_have_css("background-color", standard_color)
        assert fill.evaluate("fill => fill.style.width") == "25%"
        expect(older).to_be_visible()
        progress_snapshot = {"phase": "processing", "downloaded_bytes": 4096, "total_bytes": None}
        expect(status).to_have_text("Processing YouTube audio…")
        expect(progress).to_be_hidden()
        allow_completion = True
        result = recent.get_by_role("link", name=filename)
        expect(result).to_be_visible()
        expect(progress).to_have_attribute("aria-valuenow", "100")
        assert fill.evaluate("fill => fill.style.width") == "100%"
        expect(result).to_have_text(filename)
        expect(result).to_have_js_property("href", f"{macos_sidebar_server_url}{media_path}")
        expect(recent.get_by_role("link")).to_have_text([filename, older_name])
        assert submitted == [{"url": url, "mode": "audio"}]
        assert status_requests >= 3

        url_input.press("Tab")
        expect(clear_url).to_be_focused()
        clear_url.press("Space")
        expect(url_input).to_have_value("")
        expect(url_input).to_be_focused()
        expect(clear_url).to_be_hidden()
        expect(result).to_be_visible()
        assert submitted == [{"url": url, "mode": "audio"}]

        fail_next = True
        url_input.fill(url)
        submit.click()
        expect(panel.get_by_role("status")).to_have_text("A YouTube download is already running.")
        expect(progress).to_be_hidden()
        expect(result).to_be_visible()

        trigger.click()
        expect(youtube).to_have_attribute("aria-selected", "true")
        sidebar.locator('[data-cache-source-switcher-option="chatgpt"]').click()
        expect(panel).to_be_hidden()
        expect(browser_controls).to_be_visible()
        expect(trigger).to_have_accessible_name("Cache source: ChatGPT Chat")
        expect(page).to_have_url(media_url)

        trigger.click()
        youtube.click()
        expect(result).to_be_visible()
        sidebar.get_by_role("radio", name="Text", exact=True).click()
        expect(panel).to_be_hidden()
        expect(browser_controls).to_be_visible()
        expect(trigger).to_have_accessible_name("Cache source: ChatGPT Chat")
        expect(page).to_have_url(f"{macos_sidebar_server_url}/cache/chatgpt/text/safari")
        trigger.click()
        expect(youtube).to_have_count(0)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_youtube_selection_survives_reload_and_respects_cache_history(
    disposable_browser: Browser,
    macos_sidebar_server_url: str,
) -> None:
    context = disposable_browser.new_context(viewport={"width": 1280, "height": 900})
    context.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "browser": "safari", "logged_in": True, "can_download": True, "account_name": "Signed in",
    }))
    page = context.new_page()
    text_url = f"{macos_sidebar_server_url}/cache/x/text/safari"
    media_url = f"{macos_sidebar_server_url}/cache/x/media/safari"
    sidebar = page.locator("#app_sidebar")
    trigger = sidebar.locator("[data-cache-source-switcher-trigger]")
    panel = sidebar.get_by_role("region", name="YouTube cache")

    def expect_youtube_audio() -> None:
        expect(page).to_have_url(media_url)
        expect(trigger).to_have_accessible_name("Cache source: YouTube")
        expect(sidebar.get_by_role("radio", name="Media", exact=True)).to_have_attribute("aria-checked", "true")
        expect(panel.get_by_role("radio", name="Audio")).to_be_checked()
        expect(panel.locator(".segmented-control")).to_have_attribute("data-segmented-active-index", "1")
        expect(panel.get_by_role("button", name="Cache audio")).to_be_visible()

    try:
        page.goto(text_url, wait_until="domcontentloaded")
        toggle = page.locator("#sidebar_toggle")
        if toggle.is_visible() and toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
        sidebar.get_by_role("radio", name="Media", exact=True).click()
        expect(page).to_have_url(media_url)
        trigger.click()
        sidebar.get_by_role("option", name="YouTube", exact=True).click()
        panel.get_by_role("radio", name="Video").press("ArrowRight")
        expect_youtube_audio()

        page.reload(wait_until="domcontentloaded")
        expect_youtube_audio()
        expect(panel.get_by_role("textbox", name="YouTube URL")).to_have_value("")

        other_tab = context.new_page()
        other_tab.goto(media_url, wait_until="domcontentloaded")
        expect(other_tab.locator("[data-cache-source-switcher-trigger]")).to_have_accessible_name("Cache source: X")
        other_tab.close()

        sidebar.get_by_role("radio", name="Text", exact=True).click()
        page.reload(wait_until="domcontentloaded")
        expect(page).to_have_url(text_url)
        expect(panel).to_be_hidden()
        expect(trigger).to_have_accessible_name("Cache source: X")

        page.go_back(wait_until="domcontentloaded")
        expect_youtube_audio()
        page.go_forward(wait_until="domcontentloaded")
        expect(page).to_have_url(text_url)
        expect(panel).to_be_hidden()
        page.go_back(wait_until="domcontentloaded")
        expect_youtube_audio()

        trigger.click()
        sidebar.get_by_role("option", name="X", exact=True).click()
        page.reload(wait_until="domcontentloaded")
        expect(page).to_have_url(media_url)
        expect(trigger).to_have_accessible_name("Cache source: X")
        expect(panel).to_be_hidden()
    finally:
        context.close()
