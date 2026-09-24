"""Platform browser launch and login handoff transport for Computer Use Agent.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

from collections.abc import Callable
import os
import shutil
import subprocess
import sys
from typing import Any

from ..agent_session_sources import normalize_agent_conversation_url
from ..browser_executables import resolve_windows_browser_executable
from ..browser_sessions import BrowserDescriptor, browser_descriptors
from ..config import CrawlConfig, is_windows_host
from .platform_catalog import (
    AGENT_PLATFORM_BY_KEY,
    DEFAULT_AGENT_PLATFORM,
    SUPPORTED_AGENT_PLATFORMS,
    SUPPORTED_BROWSERS,
    _normalize_web_agent_target,
    _platform_home_url,
)


WindowsExecutableResolver = Callable[[str], str | None]
WindowsHostCheck = Callable[[], bool]
BrowserDescriptorLoader = Callable[[CrawlConfig], dict[str, BrowserDescriptor]]
BrowserOpener = Callable[..., dict[str, Any]]


def open_agent_in_default_browser(
    platform: str = DEFAULT_AGENT_PLATFORM,
    target_url: str = "",
) -> dict[str, Any]:
    """Open one trusted Web Agent target through the host system's default browser."""
    selected_platform = str(platform or DEFAULT_AGENT_PLATFORM).strip().lower()
    destination = _normalize_web_agent_target(selected_platform, target_url)
    process_options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "darwin":
        command = ["/usr/bin/open", destination]
        process_options["start_new_session"] = True
    elif os.name == "nt":
        command = ["cmd.exe", "/c", "start", "", destination]
    else:
        command = ["xdg-open", destination]
        process_options["start_new_session"] = True

    try:
        subprocess.Popen(command, **process_options)
    except OSError as exc:
        raise RuntimeError(
            f"Could not open {selected_platform.title()} in the system default browser: {exc}"
        ) from exc

    return {
        "opened": True,
        "platform": selected_platform,
        "url": destination,
        "targeted_conversation": bool(
            normalize_agent_conversation_url(selected_platform, target_url)
        ),
    }


def _open_login_in_debug_browser(
    selected_platform: str,
    selected_browser: str,
    destination: str,
) -> dict[str, Any]:
    """Open the login destination in the project-owned debug browser window.

    Playwright ``connect_over_cdp`` enables Runtime on every page and restarts
    Cloudflare Turnstile. Login therefore uses Chromium's HTTP endpoints and
    leaves an in-progress challenge page untouched.
    """
    from ..agent_debug_browser import (
        activate_debug_browser_target,
        bring_debug_browser_to_front,
        debug_browser_command_line,
        debug_browser_lock,
        ensure_debug_browser,
        list_debug_browser_page_targets,
        open_debug_browser_url,
        restart_debug_browser,
    )
    from ..browser_sessions import (
        debug_browser_http_verification_status,
        http_target_shows_security_verification,
    )
    from ..config import is_macos_host

    application = "Microsoft Edge" if selected_browser == "edge" else "Google Chrome"
    with debug_browser_lock(selected_browser):
        ensure_debug_browser(selected_browser)
        challenge = debug_browser_http_verification_status(
            selected_browser,
            application,
            AGENT_PLATFORM_BY_KEY.get(selected_platform, {}).get("label", "the open site"),
        )
        if challenge is not None:
            command = debug_browser_command_line(selected_browser)
            if is_macos_host() and "--disable-extensions" in command:
                restart_debug_browser(selected_browser, start_url=destination)
            else:
                for target in list_debug_browser_page_targets(selected_browser):
                    if http_target_shows_security_verification(target):
                        activate_debug_browser_target(
                            selected_browser,
                            str(target.get("id") or ""),
                        )
                        break
        else:
            open_debug_browser_url(selected_browser, destination)
        bring_debug_browser_to_front(selected_browser)
    return {
        "opened": True,
        "platform": selected_platform,
        "browser": selected_browser,
        "application": application,
        "url": destination,
        "targeted_conversation": bool(
            normalize_agent_conversation_url(selected_platform, destination)
        ),
        "background": False,
        "debug_browser": True,
    }


def open_agent_in_browser(
    platform: str = DEFAULT_AGENT_PLATFORM,
    browser: str = "edge",
    target_url: str = "",
    *,
    background: bool = True,
    config: CrawlConfig | None = None,
    _windows_host_check: WindowsHostCheck | None = None,
    _browser_descriptor_loader: BrowserDescriptorLoader | None = None,
    _windows_executable_resolver: WindowsExecutableResolver | None = None,
) -> dict[str, Any]:
    """Open one trusted Web Agent target in the explicitly selected browser."""
    selected_platform = str(platform or DEFAULT_AGENT_PLATFORM).strip().lower()
    selected_browser = str(browser or "edge").strip().lower()
    if selected_browser not in SUPPORTED_BROWSERS:
        raise ValueError("The Agent browser must be Safari, Edge, or Chrome.")

    destination = _normalize_web_agent_target(selected_platform, target_url)
    process_options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    application = selected_browser.title()

    if sys.platform == "darwin":
        application = {
            "safari": "Safari",
            "edge": "Microsoft Edge",
            "chrome": "Google Chrome",
        }[selected_browser]
        if background and selected_browser in {"edge", "chrome"}:
            command = [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                "set destinationURL to item 1 of argv",
                "-e",
                f'tell application "{application}"',
                "-e",
                "repeat with existingWindow in windows",
                "-e",
                "repeat with existingTab in tabs of existingWindow",
                "-e",
                "if URL of existingTab is destinationURL then return",
                "-e",
                "end repeat",
                "-e",
                "end repeat",
                "-e",
                "set handoffWindow to make new window",
                "-e",
                "set URL of active tab of handoffWindow to destinationURL",
                "-e",
                "end tell",
                "-e",
                "end run",
                destination,
            ]
        else:
            command = ["/usr/bin/open"]
            if background:
                command.append("-g")
            command.extend(["-a", application, destination])
        process_options["start_new_session"] = True
    elif (_windows_host_check or is_windows_host)():
        if selected_browser == "safari":
            raise RuntimeError("Safari is not available for a traditional Windows handoff.")
        application = "Microsoft Edge" if selected_browser == "edge" else "Google Chrome"
        executable_resolver = _windows_executable_resolver or resolve_windows_browser_executable
        resolved_executable = executable_resolver(selected_browser)
        if resolved_executable is None:
            raise RuntimeError(f"{application} could not be found on this host.")
        descriptor_loader = _browser_descriptor_loader or browser_descriptors
        descriptor = descriptor_loader(config if config is not None else CrawlConfig())[
            selected_browser
        ]
        command = [
            resolved_executable,
            f"--user-data-dir={descriptor.user_data_dir}",
            f"--profile-directory={descriptor.profile_directory}",
            destination,
        ]
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess,
            "DETACHED_PROCESS",
            0,
        )
        if creation_flags:
            process_options["creationflags"] = creation_flags
    else:
        if selected_browser == "safari":
            raise RuntimeError("Safari is not available for a traditional Linux handoff.")
        application = "Microsoft Edge" if selected_browser == "edge" else "Google Chrome"
        executable = "microsoft-edge" if selected_browser == "edge" else "google-chrome"
        resolved_executable = shutil.which(executable)
        if not resolved_executable:
            raise RuntimeError(f"{application} could not be found on this host.")
        command = [resolved_executable, destination]
        process_options["start_new_session"] = True

    try:
        subprocess.Popen(command, **process_options)
    except OSError as exc:
        raise RuntimeError(
            f"Could not open {AGENT_PLATFORM_BY_KEY[selected_platform]['label']} in "
            f"{application}: {exc}"
        ) from exc

    return {
        "opened": True,
        "platform": selected_platform,
        "browser": selected_browser,
        "application": application,
        "url": destination,
        "targeted_conversation": bool(
            normalize_agent_conversation_url(selected_platform, target_url)
        ),
        "background": bool(background),
    }


def open_browser_for_login(
    platform: str,
    browser: str,
    *,
    config: CrawlConfig | None = None,
    use_debug_profile: bool = True,
    _windows_host_check: WindowsHostCheck | None = None,
    _browser_opener: BrowserOpener | None = None,
    _debug_login_opener: BrowserOpener | None = None,
) -> dict[str, Any]:
    """Open a visible supported browser at its platform home for sign-in.

    Cache pages and macOS ChatGPT Edge Agent pass ``use_debug_profile=False``
    because their workers clone the daily profile. Signing in to the project
    debug profile would not reach those workers.
    """
    selected_platform = str(platform or "").strip().lower()
    selected_browser = str(browser or "").strip().lower()
    if selected_platform not in SUPPORTED_AGENT_PLATFORMS:
        raise ValueError("The Agent platform must be ChatGPT, Gemini, Grok, or Claude.")
    if selected_browser not in SUPPORTED_BROWSERS:
        raise ValueError("The Agent browser must be Safari, Edge, or Chrome.")
    if sys.platform != "darwin" and not (_windows_host_check or is_windows_host)():
        raise RuntimeError("Browser login handoff is only supported on macOS and Windows.")
    # Debug-profile callers must sign in to that profile before CDP task reuse.
    from ..agent_debug_browser import debug_browser_supported

    if use_debug_profile and debug_browser_supported(selected_browser):
        debug_login_opener = _debug_login_opener or _open_login_in_debug_browser
        return debug_login_opener(
            selected_platform,
            selected_browser,
            _platform_home_url(selected_platform),
        )
    browser_opener = _browser_opener or open_agent_in_browser
    return browser_opener(
        selected_platform,
        selected_browser,
        _platform_home_url(selected_platform),
        background=sys.platform == "darwin",
        config=config,
    )


def open_chatgpt_in_default_browser(
    target_url: str = "",
    *,
    _default_browser_opener: BrowserOpener | None = None,
) -> dict[str, Any]:
    """Open a trusted ChatGPT target through the host system's default browser."""
    browser_opener = _default_browser_opener or open_agent_in_default_browser
    result = browser_opener("chatgpt", target_url)
    result.pop("platform", None)
    return result


__all__ = [
    "open_agent_in_browser",
    "open_agent_in_default_browser",
    "open_browser_for_login",
    "open_chatgpt_in_default_browser",
    "resolve_windows_browser_executable",
]
