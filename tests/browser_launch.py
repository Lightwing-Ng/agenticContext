"""Keep disposable Windows browsers compatible with Chromium's TCP allocator.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from pathlib import Path
import sys

from playwright.sync_api import Browser, BrowserType


_DISABLED_FEATURES = "--disable-features="
_TCP_FEATURE = "TcpPortRandomizationWin"
_PROFILE = "--user-data-dir="


def _command_line(browser: Browser) -> list[str]:
    session = browser.new_browser_cdp_session()
    try:
        arguments = session.send("Browser.getBrowserCommandLine")["arguments"]
    except BaseException as error:
        try:
            session.detach()
        except Exception as cleanup_error:
            error.add_note(f"CDP session cleanup failed: {type(cleanup_error).__name__}.")
        raise
    else:
        session.detach()
    return arguments


def _without_feature_and_owned_profile(arguments: list[str]) -> list[str]:
    profiles = [argument for argument in arguments if argument.startswith(_PROFILE)]
    assert len(profiles) == 1, "The disposable browser must own one temporary profile."
    name = Path(profiles[0].removeprefix(_PROFILE)).name
    assert name.startswith("playwright_chromiumdev_profile-"), name
    return [
        argument for argument in arguments
        if not argument.startswith((_DISABLED_FEATURES, _PROFILE))
    ]


def _close_after_error(browser: Browser, error: BaseException) -> None:
    try:
        browser.close()
    except Exception as cleanup_error:
        error.add_note(f"Disposable browser cleanup failed: {type(cleanup_error).__name__}.")


def launch_test_browser(
    browser_type: BrowserType,
    *,
    channel: str | None = None,
    args: list[str] | None = None,
    windows: bool | None = None,
) -> Browser:
    """Backport Playwright #43605 without replacing its other default switches.

    Python Playwright 1.64.1 does not include the upstream fix. Obtain the actual
    defaults from a disposable blank browser rather than copying a versioned list.
    The explicit host argument lets the real CDP contract run on every test host.
    This helper is never used by production browser launches or user profiles.
    https://github.com/microsoft/playwright/pull/43605
    """
    options = {"headless": True}
    if channel is not None:
        options["channel"] = channel
    caller_args = list(args or [])
    if args is not None:
        options["args"] = caller_args
    if not (sys.platform == "win32" if windows is None else windows):
        return browser_type.launch(**options)

    assert not any(argument.startswith(_DISABLED_FEATURES) for argument in caller_args), (
        "Caller arguments must not replace the complete default feature list."
    )
    # CDP requires this inspection flag. Keep it confined to the blank probe
    # unless the caller explicitly requested it for an effective-arguments test.
    probe_args = caller_args if "--enable-automation" in caller_args else [
        *caller_args, "--enable-automation",
    ]
    probe = browser_type.launch(**{**options, "args": probe_args})
    try:
        baseline = _command_line(probe)
        default_switches = [
            argument for argument in baseline if argument.startswith(_DISABLED_FEATURES)
        ]
        assert len(default_switches) == 1, "Expected one complete default feature switch."
        original = default_switches[0]
        features = original.removeprefix(_DISABLED_FEATURES).split(",")
        assert features and all(features), "The default feature list must be complete."
    except BaseException as error:
        _close_after_error(probe, error)
        raise
    else:
        probe.close()

    merged = original if _TCP_FEATURE in features else f"{original},{_TCP_FEATURE}"
    browser = browser_type.launch(
        **{**options, "ignore_default_args": [original], "args": [*caller_args, merged]},
    )
    if "--enable-automation" not in caller_args:
        return browser
    try:
        effective = _command_line(browser)
        actual_switches = [
            argument for argument in effective if argument.startswith(_DISABLED_FEATURES)
        ]
        assert actual_switches == [merged], "The effective feature list was replaced."
        assert _without_feature_and_owned_profile(effective) == (
            _without_feature_and_owned_profile(baseline)
        ), "A default browser switch changed beyond the temporary profile and TCP feature."
    except BaseException as error:
        _close_after_error(browser, error)
        raise
    return browser
