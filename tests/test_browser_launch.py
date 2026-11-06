"""Verify the Windows compatibility option against real disposable Chromium.

Code version: v1.0.0-codex.0
"""

from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import Browser, sync_playwright
import pytest

from tests import browser_launch


def _arguments(browser):
    session = browser.new_browser_cdp_session()
    try:
        return session.send("Browser.getBrowserCommandLine")["arguments"]
    finally:
        session.detach()


def test_windows_compatibility_preserves_real_defaults_and_closes_the_probe(monkeypatch):
    with sync_playwright() as playwright:
        original = playwright.chromium.launch
        browsers = []
        launches = []
        arguments = []

        def observed(**options):
            if browsers:
                assert not browsers[-1].is_connected(), "The probe must close first."
            browser = original(**options)
            browsers.append(browser)
            launches.append(options)
            arguments.append(_arguments(browser))
            return browser

        monkeypatch.setattr(playwright.chromium, "launch", observed)
        browser = browser_launch.launch_test_browser(
            playwright.chromium, args=["--enable-automation"], windows=True,
        )
        try:
            assert len(browsers) == 2
            assert not browsers[0].is_connected()
            assert browsers[1].is_connected()
            baseline, actual = arguments
            default_switches = [item for item in baseline if item.startswith("--disable-features=")]
            actual_switches = [item for item in actual if item.startswith("--disable-features=")]
            assert len(default_switches) == len(actual_switches) == 1
            defaults = default_switches[0].split("=", 1)[1].split(",")
            expected = defaults if "TcpPortRandomizationWin" in defaults else [
                *defaults, "TcpPortRandomizationWin",
            ]
            assert actual_switches[0].split("=", 1)[1].split(",") == expected
            assert launches[0] == {"headless": True, "args": ["--enable-automation"]}
            assert launches[1]["ignore_default_args"] == default_switches
            assert launches[1]["args"] == ["--enable-automation", *actual_switches]
            for items in arguments:
                profiles = [item for item in items if item.startswith("--user-data-dir=")]
                assert len(profiles) == 1
                assert Path(profiles[0].split("=", 1)[1]).name.startswith(
                    "playwright_chromiumdev_profile-"
                )
            def preserved(items):
                return [
                    item for item in items
                    if not item.startswith(("--disable-features=", "--user-data-dir="))
                ]
            assert preserved(actual) == preserved(baseline)
        finally:
            browser.close()
        assert all(not item.is_connected() for item in browsers)


@pytest.mark.parametrize("channel", [None, "chrome", "msedge"])
def test_default_replacement_keeps_inspection_flag_out_of_caller_options(monkeypatch, channel):
    """Option plumbing is separate from the real CDP contract's explicit inspection flag."""
    launches = []
    browsers = []

    def launch(**options):
        browser = SimpleNamespace(closed=False)
        browser.close = lambda: setattr(browser, "closed", True)
        launches.append(options)
        browsers.append(browser)
        return browser

    original = "--disable-features=ExistingOne,ExistingTwo"
    monkeypatch.setattr(browser_launch, "_command_line", lambda _browser: [original])
    result = browser_launch.launch_test_browser(
        SimpleNamespace(launch=launch), channel=channel, windows=True,
    )
    selected = {"headless": True, **({"channel": channel} if channel is not None else {})}
    assert launches == [
        {**selected, "args": ["--enable-automation"]},
        {
            **selected,
            "ignore_default_args": [original],
            "args": [f"{original},TcpPortRandomizationWin"],
        },
    ]
    assert browsers[0].closed
    assert result is browsers[1]
    assert not result.closed


def test_other_hosts_launch_once_without_adding_options(monkeypatch):
    with sync_playwright() as playwright:
        original = playwright.chromium.launch
        launches = []

        def observed(**options):
            launches.append(options)
            return original(**options)

        monkeypatch.setattr(playwright.chromium, "launch", observed)
        browser = browser_launch.launch_test_browser(playwright.chromium, windows=False)
        try:
            assert launches == [{"headless": True}]
            assert browser.is_connected()
        finally:
            browser.close()


@pytest.mark.parametrize("failed_inspection", [1, 2])
def test_failed_inspection_closes_real_browsers_and_preserves_the_error(
    monkeypatch, failed_inspection,
):
    failure = RuntimeError("Controlled command-line inspection failure")
    with sync_playwright() as playwright:
        original = playwright.chromium.launch
        original_session = Browser.new_browser_cdp_session
        browsers = []
        inspected = 0

        def observed(**options):
            browser = original(**options)
            browsers.append(browser)
            return browser

        def controlled_session(browser):
            session = original_session(browser)
            original_send = session.send
            original_detach = session.detach
            failed = False

            def send(method, *args, **options):
                nonlocal inspected, failed
                if method == "Browser.getBrowserCommandLine":
                    inspected += 1
                    if inspected == failed_inspection:
                        assert browser.is_connected()
                        failed = True
                        raise failure
                return original_send(method, *args, **options)

            def detach():
                original_detach()
                if failed:
                    raise RuntimeError("Controlled session cleanup failure")

            monkeypatch.setattr(session, "send", send)
            monkeypatch.setattr(session, "detach", detach)
            return session

        monkeypatch.setattr(playwright.chromium, "launch", observed)
        monkeypatch.setattr(Browser, "new_browser_cdp_session", controlled_session)
        with pytest.raises(RuntimeError) as raised:
            browser_launch.launch_test_browser(
                playwright.chromium, args=["--enable-automation"], windows=True,
            )
        assert raised.value is failure
        assert any("CDP session cleanup failed" in note for note in failure.__notes__)
        assert len(browsers) == failed_inspection
        assert all(not browser.is_connected() for browser in browsers)
