"""Tests for browser-independent X parsing and session helpers.

Code version: v1.7.4-codex.1
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.browser_sessions import (
    CHROMIUM_WINDOW_MODE_TASK_STAGE,
    BrowserDescriptor,
    build_chromium_launch_args,
    clone_browser_profile,
    extract_json_string_field,
    extract_x_account_from_source,
    detect_safari_x_account_handle,
    fetch_safari_page_snapshot,
    goto_with_retry,
    _housekeep_stale_chromium_profiles,
    is_grok_security_verification_page,
    launch_chromium_context,
    parse_grok_account_label,
    probe_browser_session,
)
from app.core.config import CrawlConfig
from app.core.scraper import (
    build_likes_request_template,
    build_x_likes_url,
    collect_liked_tweet_urls_via_safari,
    extract_account_handle_from_urlish,
    normalize_status_url,
    parse_likes_timeline_page,
    collect_liked_tweet_urls,
)
from app.core.state import TaskState


@pytest.fixture(autouse=True)
def isolated_windows_browser_executable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fake executable discovery whenever a test uses a fake Chromium launcher."""
    from app.core import computer_use_agent

    monkeypatch.setattr(
        computer_use_agent,
        "resolve_windows_browser_executable",
        lambda browser: str(tmp_path / f"synthetic-{browser}.exe"),
    )


@pytest.mark.parametrize("stop_stage", ("before_navigation", "after_error", "during_retry_wait"))
def test_goto_with_retry_does_not_navigate_again_after_stop(stop_stage: str) -> None:
    stop_requested = stop_stage == "before_navigation"
    goto_calls: list[str] = []
    waits: list[int] = []

    class Page:
        def goto(self, url: str, **_kwargs: object) -> None:
            nonlocal stop_requested
            goto_calls.append(url)
            if stop_stage == "after_error":
                stop_requested = True
            raise RuntimeError("net::ERR_NETWORK_CHANGED")

        def wait_for_timeout(self, milliseconds: int) -> None:
            nonlocal stop_requested
            waits.append(milliseconds)
            if stop_stage == "during_retry_wait":
                stop_requested = True

    goto_with_retry(
        Page(),
        "https://chatgpt.com/",
        attempts=2,
        timeout_ms=90_000,
        should_stop=lambda: stop_requested,
    )

    assert goto_calls == ([] if stop_stage == "before_navigation" else ["https://chatgpt.com/"])
    assert waits == ([1_500] if stop_stage == "during_retry_wait" else [])


def test_goto_with_retry_retries_an_exact_connection_timeout() -> None:
    goto_calls: list[str] = []
    waits: list[int] = []

    class Page:
        def goto(self, url: str, **_kwargs: object) -> None:
            goto_calls.append(url)
            if len(goto_calls) == 1:
                raise RuntimeError("Page.goto: net::ERR_CONNECTION_TIMED_OUT")

        def wait_for_timeout(self, milliseconds: int) -> None:
            waits.append(milliseconds)

    goto_with_retry(
        Page(),
        "https://gemini.google.com/app",
        attempts=2,
        timeout_ms=90_000,
    )

    assert goto_calls == [
        "https://gemini.google.com/app",
        "https://gemini.google.com/app",
    ]
    assert waits == [1_500]


def _tweet_entry(status_id: str, handle: str) -> dict[str, object]:
    return {
        "content": {
            "__typename": "TimelineTimelineItem",
            "itemContent": {
                "__typename": "TimelineTweet",
                "tweet_results": {
                    "result": {
                        "rest_id": status_id,
                        "legacy": {"id_str": status_id},
                        "core": {"user_results": {"result": {"legacy": {"screen_name": handle}}}},
                    }
                },
            },
        }
    }


def test_x_url_and_handle_parsing_rejects_reserved_and_foreign_values() -> None:
    assert build_x_likes_url("@demo") == "https://x.com/demo/likes"
    assert extract_account_handle_from_urlish("https://x.com/demo/likes") == "demo"
    assert extract_account_handle_from_urlish("/demo/media") == "demo"
    assert extract_account_handle_from_urlish("https://x.com/settings") == ""
    assert extract_account_handle_from_urlish("https://example.com/demo") == ""
    assert normalize_status_url("http://twitter.com/demo/status/123/?x=1") == "https://x.com/demo/status/123"
    assert normalize_status_url("https://x.com/i/web/status/456") == "https://x.com/i/status/456"
    with pytest.raises(RuntimeError, match="without a detected account handle"):
        build_x_likes_url("")


def test_likes_timeline_parser_extracts_tweets_and_bottom_cursor() -> None:
    payload = {
        "data": {
            "user": {
                "result": {
                    "timeline": {
                        "timeline": {
                            "instructions": [
                                {
                                    "entries": [
                                        _tweet_entry("101", "alice"),
                                        {"content": {"__typename": "TimelineTimelineCursor", "cursorType": "Bottom", "value": "cursor-2"}},
                                        _tweet_entry("102", "bob"),
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
    }

    urls, cursor = parse_likes_timeline_page(payload)

    assert urls == ["https://x.com/alice/status/101", "https://x.com/bob/status/102"]
    assert cursor == "cursor-2"


def test_authenticated_request_template_keeps_only_required_headers() -> None:
    response = SimpleNamespace(
        url="https://x.com/i/api/graphql/query/Likes?variables=%7B%22userId%22%3A%221%22%7D&features=%7B%22a%22%3Atrue%7D",
        request=SimpleNamespace(headers={"Authorization": "Bearer test", "X-Csrf-Token": "csrf", "Cookie": "private"}),
    )

    template = build_likes_request_template(response, "https://x.com/demo/likes")

    assert template.api_url_base == "https://x.com/i/api/graphql/query/Likes"
    assert template.variables == {"userId": "1"}
    assert template.headers == {"Authorization": "Bearer test", "X-Csrf-Token": "csrf", "referer": "https://x.com/demo/likes"}


def test_browser_session_parsers_do_not_require_live_browser_access() -> None:
    assert extract_json_string_field('{"givenName":"Demo User"}', "givenName") == "Demo User"
    assert extract_x_account_from_source('{"screen_name":"demo_user"}') == "demo_user"
    assert parse_grok_account_label('{"givenName":"Demo","xUsername":"demo_x"}') == "Demo (@demo_x)"
    assert is_grok_security_verification_page(
        "Just a moment...",
        (
            "Performing security verification\n"
            "This website uses a security service to protect against malicious bots.\n"
            "Performance and Security by Cloudflare"
        ),
    )
    assert not is_grok_security_verification_page(
        "Grok",
        "Ready. Found existing Grok cache: 475 assets, 429 images, 46 videos.",
    )
    with pytest.raises(ValueError, match="Unsupported platform"):
        probe_browser_session("unknown", "chrome", CrawlConfig())
    with pytest.raises(ValueError, match="Unsupported browser"):
        probe_browser_session("x", "firefox", CrawlConfig())


def test_browser_probe_preserves_playwright_exception_class_in_status_message(macos_host) -> None:
    class TargetClosedError(RuntimeError):
        pass

    with patch(
        "app.core.browser_sessions._probe_chromium_x_session",
        side_effect=TargetClosedError("Target page, context or browser has been closed"),
    ):
        result = probe_browser_session("x", "edge", CrawlConfig())

    assert result["message"] == (
        "TargetClosedError: Target page, context or browser has been closed"
    )


def test_sync_playwright_probe_lifecycle_is_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.browser_sessions as browser_sessions

    active = 0
    maximum_active = 0
    state_lock = threading.Lock()
    first_entered = threading.Event()
    second_attempted = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    class FakePlaywrightLifecycle:
        def __enter__(self):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            return object()

        def __exit__(self, _exc_type, _exc_value, _traceback):
            nonlocal active
            with state_lock:
                active -= 1
            return False

    monkeypatch.setattr(
        browser_sessions,
        "sync_playwright_or_error",
        lambda: FakePlaywrightLifecycle(),
    )

    def first_probe() -> None:
        with browser_sessions._serialized_sync_playwright():
            first_entered.set()
            release_first.wait(timeout=2)

    def second_probe() -> None:
        second_attempted.set()
        with browser_sessions._serialized_sync_playwright():
            second_entered.set()

    first_thread = threading.Thread(target=first_probe)
    second_thread = threading.Thread(target=second_probe)
    first_thread.start()
    assert first_entered.wait(timeout=1)
    second_thread.start()
    assert second_attempted.wait(timeout=1)
    assert not second_entered.wait(timeout=0.05)
    release_first.set()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert maximum_active == 1


def test_background_chromium_launch_args_keep_the_window_offscreen() -> None:
    descriptor = BrowserDescriptor(
        browser_id="edge",
        label="Edge",
        icon_filename="images/browser.edge.png",
        engine="chromium",
        user_data_dir=Path("/tmp/edge"),
        profile_directory="Default",
        channel="msedge",
    )

    assert build_chromium_launch_args(descriptor, background_window=False) == ["--profile-directory=Default"]
    assert build_chromium_launch_args(descriptor) == [
        "--profile-directory=Default",
        "--window-position=-32000,-32000",
        "--window-size=1280,900",
        "--start-minimized",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--noerrdialogs",
        "--disable-notifications",
        "--disable-prompt-on-repost",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]
    assert build_chromium_launch_args(
        descriptor,
        window_mode=CHROMIUM_WINDOW_MODE_TASK_STAGE,
    ) == [
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--noerrdialogs",
        "--disable-notifications",
        "--disable-prompt-on-repost",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]


def test_gemini_browser_probe_routes_through_the_shared_browser_registry(macos_host) -> None:
    with patch(
        "app.core.browser_sessions._probe_gemini_session",
        return_value={
            "logged_in": True,
            "can_download": True,
            "account_name": "Google account",
            "message": "Safari verified Gemini.",
        },
    ) as probe:
        result = probe_browser_session("gemini", "safari", CrawlConfig())

    assert result["logged_in"] is True
    assert result["can_download"] is True
    assert result["browser"] == "safari"
    probe.assert_called_once()


def test_gemini_browser_probe_fails_closed_when_the_current_region_is_unsupported(
    macos_host,
) -> None:
    with patch(
        "app.core.browser_sessions._probe_gemini_session",
        side_effect=RuntimeError(
            "Gemini Web is not available in the selected browser's current region."
        ),
    ) as probe:
        result = probe_browser_session("gemini", "edge", CrawlConfig())

    assert result["logged_in"] is False
    assert result["can_download"] is False
    assert result["account_name"] == ""
    assert result["message"] == (
        "RuntimeError: Gemini Web is not available in the selected browser's current region."
    )
    probe.assert_called_once()


def test_claude_browser_probe_routes_through_the_shared_browser_registry() -> None:
    with patch(
        "app.core.browser_sessions._probe_claude_session",
        return_value={
            "logged_in": False,
            "can_download": False,
            "account_name": "Claude account restricted",
            "message": "Edge reported that the Claude account is restricted or unavailable.",
        },
    ) as probe:
        result = probe_browser_session("claude", "edge", CrawlConfig())

    assert result["logged_in"] is False
    assert result["can_download"] is False
    assert result["account_name"] == "Claude account restricted"
    probe.assert_called_once()


@pytest.mark.parametrize("hydrated_count", (1, 2))
def test_claude_probe_waits_for_hydration_before_requiring_unique_composer(hydrated_count):
    """A delayed composer must be awaited, while ambiguous composers stay blocked."""
    from app.core.browser_sessions import _probe_claude_session

    hydrated = False
    composer = MagicMock()

    def wait_for_composer(**_kwargs):
        nonlocal hydrated
        hydrated = True

    composer.first.wait_for.side_effect = wait_for_composer
    composer.count.side_effect = lambda: hydrated_count if hydrated else 0
    page = MagicMock()
    page.locator.side_effect = lambda selector: (
        SimpleNamespace(inner_text=lambda **_kwargs: "Welcome back")
        if selector == "body" else composer
    )
    context = MagicMock()
    context.pages = [page]
    descriptor = SimpleNamespace(engine="chromium", label="Edge")
    with (
        patch("app.core.browser_sessions._serialized_sync_playwright"),
        patch("app.core.browser_sessions.launch_chromium_context") as launch,
        patch("app.core.browser_sessions.goto_with_retry"),
    ):
        launch.return_value.__enter__.return_value = context
        result = _probe_claude_session(descriptor)

    assert hydrated
    assert result["can_download"] is (hydrated_count == 1)
    composer.first.wait_for.assert_called_once_with(state="visible", timeout=20_000)
    assert launch.call_args.kwargs["headless"] is False
    assert launch.call_args.kwargs["background_window"] is True


@pytest.mark.parametrize("host_platform", ("darwin", "win32", "linux"))
def test_chromium_context_defaults_to_an_isolated_background_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_platform: str,
) -> None:
    monkeypatch.setattr("app.core.browser_sessions.is_macos_host", lambda: host_platform == "darwin")
    source_user_data_dir = tmp_path / "Edge"
    source_profile_dir = source_user_data_dir / "Default"
    source_profile_dir.mkdir(parents=True)
    (source_profile_dir / "Preferences").write_text("{}", encoding="utf-8")
    descriptor = BrowserDescriptor(
        browser_id="edge",
        label="Edge",
        icon_filename="images/browser.edge.png",
        engine="chromium",
        user_data_dir=source_user_data_dir,
        profile_directory="Default",
        channel="msedge",
    )
    context = SimpleNamespace(close=lambda: None)

    class Chromium:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def launch_persistent_context(self, **kwargs):
            self.calls.append(kwargs)
            return context

    chromium = Chromium()
    playwright = SimpleNamespace(chromium=chromium)

    with launch_chromium_context(playwright, descriptor, headless=False) as launched_context:
        assert launched_context is context
        temporary_profile_root = Path(str(chromium.calls[0]["user_data_dir"])).parent
        assert temporary_profile_root.is_dir()

    assert len(chromium.calls) == 1
    launch_kwargs = chromium.calls[0]
    assert Path(str(launch_kwargs["user_data_dir"])) != source_user_data_dir
    assert launch_kwargs["headless"] is False
    assert launch_kwargs["args"] == build_chromium_launch_args(descriptor)
    assert "--window-position=-32000,-32000" in launch_kwargs["args"]
    assert "--start-minimized" in launch_kwargs["args"]
    assert not temporary_profile_root.exists()


@pytest.mark.parametrize("macos_host", (True, False))
def test_silent_edge_chromium_context_is_backgrounded_without_stealing_focus(
    tmp_path: Path,
    macos_host: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import computer_use_agent

    monkeypatch.setattr("app.core.browser_sessions.is_macos_host", lambda: macos_host)
    monkeypatch.setattr(computer_use_agent, "_capture_macos_frontmost_application", lambda: "Editor")
    restored = []
    monkeypatch.setattr(computer_use_agent, "_restore_macos_frontmost_application_after_task_stage", lambda *args: restored.append(args))
    source_user_data_dir = tmp_path / "Edge"
    source_profile_dir = source_user_data_dir / "Default"
    source_profile_dir.mkdir(parents=True)
    (source_profile_dir / "Preferences").write_text("{}", encoding="utf-8")
    descriptor = BrowserDescriptor(
        browser_id="edge",
        label="Edge",
        icon_filename="images/browser.edge.png",
        engine="chromium",
        user_data_dir=source_user_data_dir,
        profile_directory="Default",
        channel="msedge",
    )
    context = SimpleNamespace(close=lambda: None)

    class Chromium:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def launch_persistent_context(self, **kwargs):
            self.calls.append(kwargs)
            return context

    chromium = Chromium()
    playwright = SimpleNamespace(chromium=chromium)

    with launch_chromium_context(
        playwright,
        descriptor,
        headless=False,
        silent=True,
    ) as launched_context:
        assert launched_context is context

    launch_kwargs = chromium.calls[0]
    assert launch_kwargs["headless"] is False
    assert "--profile-directory=Default" in launch_kwargs["args"]
    assert ("--window-position=-32000,-32000" in launch_kwargs["args"]) is (not macos_host)
    assert ("--start-minimized" in launch_kwargs["args"]) is (not macos_host)
    assert restored == ([("Editor", "Microsoft Edge")] if macos_host else [])


@pytest.mark.parametrize(
    ("browser_id", "channel", "dir_name"),
    (
        ("edge", "msedge", "Edge"),
        ("chrome", "chrome", "Chrome"),
    ),
)
@pytest.mark.parametrize("macos_host", (True, False))
@pytest.mark.parametrize("silent", (True, False))
def test_task_stage_chromium_context_is_not_forced_back_offscreen_by_silent_mode(
    tmp_path: Path,
    macos_host: bool,
    silent: bool,
    browser_id: str,
    channel: str,
    dir_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.core.browser_sessions.is_macos_host", lambda: macos_host)
    monkeypatch.setattr("app.core.computer_use_agent._capture_macos_frontmost_application", lambda: "")
    monkeypatch.setattr("app.core.computer_use_agent._restore_macos_frontmost_application_after_task_stage", lambda *_: None)
    source_user_data_dir = tmp_path / dir_name
    source_profile_dir = source_user_data_dir / "Default"
    source_profile_dir.mkdir(parents=True)
    (source_profile_dir / "Preferences").write_text("{}", encoding="utf-8")
    descriptor = BrowserDescriptor(
        browser_id=browser_id,
        label=browser_id.title(),
        icon_filename=f"images/browser.{browser_id}.png",
        engine="chromium",
        user_data_dir=source_user_data_dir,
        profile_directory="Default",
        channel=channel,
    )
    context = SimpleNamespace(close=lambda: None)

    class Chromium:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def launch_persistent_context(self, **kwargs):
            self.calls.append(kwargs)
            return context

    chromium = Chromium()
    playwright = SimpleNamespace(chromium=chromium)

    with launch_chromium_context(
        playwright,
        descriptor,
        headless=False,
        silent=silent,
        window_mode=CHROMIUM_WINDOW_MODE_TASK_STAGE,
    ):
        pass

    assert len(chromium.calls) == 1
    assert chromium.calls[0]["headless"] is False
    clone_path = Path(chromium.calls[0]["user_data_dir"])
    assert clone_path != source_user_data_dir
    assert not clone_path.exists()
    assert (source_profile_dir / "Preferences").read_text(encoding="utf-8") == "{}"
    launch_args = chromium.calls[0]["args"]
    assert "--window-position=-32000,-32000" not in launch_args
    assert "--window-size=1280,900" not in launch_args
    assert "--start-minimized" not in launch_args
    assert "--no-first-run" in launch_args


@pytest.mark.parametrize(
    "close_error_message",
    (
        "BrowserContext.close: Connection closed while reading from the driver",
        "BrowserContext.close: Target page, context or browser has been closed",
        "BrowserContext.close: Driver was disconnected",
    ),
)
def test_managed_chromium_context_ignores_idempotent_close_disconnects(
    tmp_path: Path,
    close_error_message: str,
) -> None:
    source_user_data_dir = tmp_path / "Edge"
    source_profile_dir = source_user_data_dir / "Default"
    source_profile_dir.mkdir(parents=True)
    (source_profile_dir / "Preferences").write_text("{}", encoding="utf-8")
    descriptor = BrowserDescriptor(
        browser_id="edge",
        label="Edge",
        icon_filename="images/browser.edge.png",
        engine="chromium",
        user_data_dir=source_user_data_dir,
        profile_directory="Default",
        channel="msedge",
    )
    context = MagicMock()
    context.close.side_effect = Exception(close_error_message)
    launch = MagicMock(return_value=context)
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch)
    )

    managed_context = launch_chromium_context(playwright, descriptor, headless=False)
    temporary_profile_root = Path(str(launch.call_args.kwargs["user_data_dir"])).parent
    with managed_context as launched_context:
        assert launched_context is context

    context.close.assert_called_once_with()
    assert not temporary_profile_root.exists()


def test_managed_chromium_context_propagates_unexpected_close_errors(
    tmp_path: Path,
) -> None:
    source_user_data_dir = tmp_path / "Edge"
    source_profile_dir = source_user_data_dir / "Default"
    source_profile_dir.mkdir(parents=True)
    (source_profile_dir / "Preferences").write_text("{}", encoding="utf-8")
    descriptor = BrowserDescriptor(
        browser_id="edge",
        label="Edge",
        icon_filename="images/browser.edge.png",
        engine="chromium",
        user_data_dir=source_user_data_dir,
        profile_directory="Default",
        channel="msedge",
    )
    close_error = RuntimeError("BrowserContext.close: Failed to save browser state")
    context = MagicMock()
    context.close.side_effect = close_error
    launch = MagicMock(return_value=context)
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch)
    )

    managed_context = launch_chromium_context(playwright, descriptor, headless=False)
    temporary_profile_root = Path(str(launch.call_args.kwargs["user_data_dir"])).parent
    with pytest.raises(RuntimeError) as exc_info:
        with managed_context:
            pass

    assert exc_info.value is close_error
    assert close_error.browser_cleanup_failed is True
    context.close.assert_called_once_with()
    assert not temporary_profile_root.exists()


@pytest.mark.parametrize("failure_stage", ("success", "task", "launch", "close"))
def test_chromium_cleanup_failure_retains_profile_and_preserves_primary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import app.core.browser_sessions as browser_sessions

    source = tmp_path / "Edge"
    source.mkdir()
    temp_root = tmp_path / "cachelikes-edge-owned"
    temp_root.mkdir()
    profile = SimpleNamespace(
        name=str(temp_root),
        cleanup=MagicMock(side_effect=PermissionError("Profile is still open")),
    )
    monkeypatch.setattr(browser_sessions, "_ACTIVE_CHROMIUM_PROFILE_ROOTS", {temp_root})
    monkeypatch.setattr(browser_sessions, "clone_browser_profile", lambda _descriptor: (temp_root, profile))
    monkeypatch.setattr(browser_sessions.tempfile, "gettempdir", lambda: str(tmp_path))
    descriptor = BrowserDescriptor(
        browser_id="edge", label="Edge", icon_filename="", engine="chromium",
        user_data_dir=source, profile_directory="Default", channel="msedge",
    )
    primary_error = ValueError(f"Original {failure_stage} failure")
    context = MagicMock()
    if failure_stage == "close":
        context.close.side_effect = primary_error
    elif failure_stage == "task":
        context.close.side_effect = RuntimeError("Secondary close failure")
    launch = MagicMock(return_value=context)
    if failure_stage == "launch":
        launch.side_effect = primary_error
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

    with pytest.raises((RuntimeError, ValueError)) as caught:
        with launch_chromium_context(playwright, descriptor, headless=False):
            if failure_stage == "task":
                raise primary_error

    if failure_stage == "success":
        assert "Could not remove" in str(caught.value)
        assert str(temp_root) in str(caught.value)
    else:
        assert caught.value is primary_error
        assert any(str(temp_root) in note for note in primary_error.__notes__)
    assert caught.value.browser_cleanup_failed is True
    assert "Could not remove" in caplog.text
    assert temp_root in browser_sessions._ACTIVE_CHROMIUM_PROFILE_ROOTS
    assert context.close.call_count == (0 if failure_stage == "launch" else 1)
    old_time = time.time() - 2 * 24 * 60 * 60
    os.utime(temp_root, (old_time, old_time))
    assert _housekeep_stale_chromium_profiles(descriptor) == 0
    assert temp_root.exists()

    profile.cleanup.side_effect = None
    browser_sessions._cleanup_cloned_browser_profile(profile)
    assert temp_root not in browser_sessions._ACTIVE_CHROMIUM_PROFILE_ROOTS


def test_clone_copy_failure_is_not_replaced_by_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.browser_sessions as browser_sessions

    source = tmp_path / "Edge"
    (source / "Default").mkdir(parents=True)
    temp_root = tmp_path / "cachelikes-edge-copy"
    temp_root.mkdir()
    profile = SimpleNamespace(
        name=str(temp_root), cleanup=MagicMock(side_effect=PermissionError("File in use")),
    )
    monkeypatch.setattr(browser_sessions, "_ACTIVE_CHROMIUM_PROFILE_ROOTS", set())
    monkeypatch.setattr(browser_sessions, "_housekeep_stale_chromium_profiles", lambda _descriptor: 0)
    monkeypatch.setattr(browser_sessions.tempfile, "TemporaryDirectory", lambda **_kwargs: profile)
    copy_error = OSError("Source profile changed during copy")
    monkeypatch.setattr(browser_sessions.shutil, "copytree", MagicMock(side_effect=copy_error))
    descriptor = BrowserDescriptor(
        browser_id="edge", label="Edge", icon_filename="", engine="chromium",
        user_data_dir=source, profile_directory="Default", channel="msedge",
    )
    with pytest.raises(OSError) as caught:
        clone_browser_profile(descriptor)
    assert caught.value is copy_error
    assert any(str(temp_root) in note for note in copy_error.__notes__)
    assert (source / "Default").is_dir()


def test_stale_chromium_profiles_retain_unknown_owners_and_other_temp_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    descriptor = BrowserDescriptor(
        browser_id="edge",
        label="Edge",
        icon_filename="images/browser.edge.png",
        engine="chromium",
        user_data_dir=tmp_path / "Edge",
        profile_directory="Default",
        channel="msedge",
    )
    stale_profile = tmp_path / "cachelikes-edge-stale"
    fresh_profile = tmp_path / "cachelikes-edge-fresh"
    unrelated_path = tmp_path / "other-application-data"
    stale_profile.mkdir()
    fresh_profile.mkdir()
    unrelated_path.mkdir()
    stale_time = time.time() - 2 * 24 * 60 * 60
    os.utime(stale_profile, (stale_time, stale_time))
    monkeypatch.setattr("app.core.browser_sessions.tempfile.gettempdir", lambda: str(tmp_path))

    with patch("app.core.browser_sessions.shutil.rmtree") as remove:
        assert _housekeep_stale_chromium_profiles(descriptor) == 0
    remove.assert_not_called()
    assert stale_profile.exists()
    assert fresh_profile.exists()
    assert unrelated_path.exists()
    assert "browser-process ownership is unverified" in caplog.text


def test_clone_browser_profile_continues_when_macos_blocks_local_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.core.browser_sessions.is_macos_host", lambda: True)
    monkeypatch.setattr("app.core.browser_sessions.is_windows_host", lambda: False)
    source_user_data_dir = tmp_path / "Edge"
    source_profile_dir = source_user_data_dir / "Default"
    source_profile_dir.mkdir(parents=True)
    (source_profile_dir / "Preferences").write_text("{}", encoding="utf-8")
    local_state = source_user_data_dir / "Local State"
    local_state.write_text("{}", encoding="utf-8")
    descriptor = BrowserDescriptor(
        browser_id="edge",
        label="Edge",
        icon_filename="images/browser.edge.png",
        engine="chromium",
        user_data_dir=source_user_data_dir,
        profile_directory="Default",
        channel="msedge",
    )

    original_read_bytes = Path.read_bytes

    def read_bytes_with_permission_error(path: Path) -> bytes:
        if path == local_state:
            raise PermissionError(1, "Operation not permitted", str(path))
        return original_read_bytes(path)

    with patch.object(Path, "read_bytes", read_bytes_with_permission_error):
        target_user_data_dir, temp_dir = clone_browser_profile(descriptor)

    try:
        assert (target_user_data_dir / "Default" / "Preferences").read_text(encoding="utf-8") == "{}"
        assert not (target_user_data_dir / "Local State").exists()
    finally:
        temp_dir.cleanup()


def test_clone_browser_profile_reports_macos_profile_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_user_data_dir = tmp_path / "Edge"
    source_profile_dir = source_user_data_dir / "Default"
    source_profile_dir.mkdir(parents=True)
    descriptor = BrowserDescriptor(
        browser_id="edge",
        label="Edge",
        icon_filename="images/browser.edge.png",
        engine="chromium",
        user_data_dir=source_user_data_dir,
        profile_directory="Default",
        channel="msedge",
    )

    monkeypatch.setattr("app.core.browser_sessions.is_macos_host", lambda: True)
    with patch("shutil.copytree", side_effect=PermissionError(1, "Operation not permitted", source_profile_dir)):
        with pytest.raises(RuntimeError, match="Full Disk Access"):
            clone_browser_profile(descriptor)


def test_clone_browser_profile_reports_windows_profile_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_user_data_dir = tmp_path / "Edge"
    source_profile_dir = source_user_data_dir / "Default"
    source_profile_dir.mkdir(parents=True)
    descriptor = BrowserDescriptor(
        browser_id="edge",
        label="Edge",
        icon_filename="images/browser.edge.png",
        engine="chromium",
        user_data_dir=source_user_data_dir,
        profile_directory="Default",
        channel="msedge",
    )

    monkeypatch.setattr("app.core.browser_sessions.is_macos_host", lambda: False)
    monkeypatch.setattr("app.core.browser_sessions.is_windows_host", lambda: True)
    with patch(
        "app.core.browser_sessions.shutil.copytree",
        side_effect=PermissionError(1, "Access is denied", str(source_profile_dir)),
    ):
        with pytest.raises(RuntimeError, match="Close any running Edge or Chrome windows"):
            clone_browser_profile(descriptor)


def test_safari_profile_link_detection_uses_the_rendered_navigation() -> None:
    page = MagicMock()
    page.evaluate.return_value = "demo_user"
    context = MagicMock()
    context.primary_page = page
    context.__enter__.return_value = context

    with patch("app.core.browser_sessions.SafariContext", return_value=context) as safari_context:
        assert detect_safari_x_account_handle(wait_seconds=1) == "demo_user"

    safari_context.assert_called_once_with("https://x.com/home")
    page.wait_for_timeout.assert_called_once_with(1_000)
    page.evaluate.assert_called_once()
    context.__exit__.assert_called_once()


def test_safari_page_snapshot_uses_a_rendered_background_window() -> None:
    page = MagicMock()
    page.url = "https://grok.com/files"
    page.content.return_value = "<html>Grok</html>"
    context = MagicMock()
    context.primary_page = page
    context.__enter__.return_value = context

    with patch("app.core.browser_sessions.SafariContext", return_value=context) as safari_context:
        snapshot = fetch_safari_page_snapshot("https://grok.com/files", wait_seconds=1)

    assert snapshot == {"url": "https://grok.com/files", "source": "<html>Grok</html>"}
    safari_context.assert_called_once_with("https://grok.com/files")
    page.wait_for_timeout.assert_called_once_with(1_000)
    page.content.assert_called_once_with(limit=500_000)
    context.__exit__.assert_called_once()


def test_safari_likes_collection_uses_window_id_targeting() -> None:
    page = MagicMock()
    page.evaluate.side_effect = [
        '["https://twitter.com/demo_user/status/123?ref=copy"]',
        "1",
        '["https://twitter.com/demo_user/status/123?ref=copy","https://x.com/demo_user/status/456"]',
        "2",
    ]
    page.url = "https://x.com/demo_user/likes"
    context = MagicMock()
    context.primary_page = page
    context.__enter__.return_value = context
    config = CrawlConfig(x_browser="safari", max_scroll_rounds=2, scroll_pause_seconds=0.2)
    state = TaskState("test")

    with patch("app.core.scraper.SafariContext", return_value=context) as safari_context:
        urls = collect_liked_tweet_urls_via_safari(
            "demo_user",
            "https://x.com/demo_user/likes",
            config,
            state,
        )

    safari_context.assert_called_once_with("https://x.com/demo_user/likes")
    assert page.wait_for_timeout.call_args_list[0].args == (8_000,)
    assert page.evaluate.call_count == 4
    context.__exit__.assert_called_once()
    assert urls == [
        "https://x.com/demo_user/status/123",
        "https://x.com/demo_user/status/456",
    ]
    assert state.snapshot()["discovered_tweets"] == 2


def test_safari_collection_prefers_navigation_handle_before_page_source(macos_host) -> None:
    config = CrawlConfig(x_browser="safari")
    state = TaskState("test")
    collected_urls = ["https://x.com/demo_user/status/123"]

    with patch("app.core.scraper.detect_safari_x_account_handle", return_value="demo_user"), patch(
        "app.core.scraper.fetch_safari_page_snapshot"
    ) as snapshot, patch(
        "app.core.scraper.collect_liked_tweet_urls_via_safari",
        return_value=collected_urls,
    ):
        handle, urls = collect_liked_tweet_urls(config, state)

    assert handle == "demo_user"
    assert urls == collected_urls
    snapshot.assert_not_called()


def test_safari_grok_probe_marks_verified_session_ready_to_download(macos_host) -> None:
    source = '{"givenName":"Demo","xUsername":"demo_x"}'

    with patch(
        "app.core.browser_sessions.fetch_safari_page_snapshot",
        return_value={"url": "https://grok.com/files", "source": source},
    ):
        result = probe_browser_session("grok", "safari", CrawlConfig())

    assert result["logged_in"] is True
    assert result["can_download"] is True
    assert result["account_name"] == "Demo (@demo_x)"
    assert result["message"] == "Safari is ready to sync Grok."
    assert result["account_name"] not in result["message"]
