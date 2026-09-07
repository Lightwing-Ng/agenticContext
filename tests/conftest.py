"""Shared pytest fixtures for isolated agenticContext tests.

Code version: v1.3.2-codex.1
"""

from __future__ import annotations

from contextvars import ContextVar
import ipaddress
import logging
import ntpath
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

import pytest

_TEST_SANDBOX = tempfile.TemporaryDirectory(prefix="cachelikes-pytest-")
_TEST_ROOT = Path(_TEST_SANDBOX.name).resolve()
_TEST_HOME = _TEST_ROOT / "home"
_TEST_RUNTIME_ROOT = _TEST_ROOT / "runtime"
_TEST_TEMP = _TEST_ROOT / "temp"
for directory in (
    _TEST_HOME, _TEST_RUNTIME_ROOT, _TEST_TEMP,
    _TEST_HOME / "AppData" / "Local", _TEST_HOME / "AppData" / "Roaming",
):
    directory.mkdir(parents=True, exist_ok=True)
# Keep attachment/editor duplicate copies out of test collection without deleting user files.
collect_ignore_glob = ["* 2.py"]
os.environ["HOME"] = str(_TEST_HOME)
os.environ["USERPROFILE"] = str(_TEST_HOME)
os.environ["HOMEDRIVE"], os.environ["HOMEPATH"] = ntpath.splitdrive(str(_TEST_HOME))
os.environ["LOCALAPPDATA"] = str(_TEST_HOME / "AppData" / "Local")
os.environ["APPDATA"] = str(_TEST_HOME / "AppData" / "Roaming")
for variable in ("TMPDIR", "TEMP", "TMP"):
    os.environ[variable] = str(_TEST_TEMP)
tempfile.tempdir = str(_TEST_TEMP)
os.environ["AGENTIC_CONTEXT_RUNTIME_ROOT"] = str(_TEST_RUNTIME_ROOT)
os.environ["AGENTIC_CONTEXT_SETTINGS_PATH"] = str(
    _TEST_RUNTIME_ROOT / "settings" / "settings.json"
)


def _loopback_host(host: object) -> bool:
    value = str(host or "").strip("[]").casefold()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _deny_external_dispatch(event: str, arguments: tuple) -> None:
    """Fail before accidental native-browser or external Python network dispatch."""
    if event in {"socket.connect", "socket.getaddrinfo"}:
        address = arguments[1] if event == "socket.connect" else arguments[0]
        if event == "socket.connect" and not isinstance(address, tuple):
            return  # Local Unix-domain sockets are not provider transport.
        host = address[0] if event == "socket.connect" else address
        port = address[1] if event == "socket.connect" else arguments[1]
        if not _loopback_host(host) or str(port) == "8666":
            raise RuntimeError("Offline tests deny external network and the user-owned service.")
    if event == "subprocess.Popen":
        executable = ntpath.basename(str(arguments[0])).casefold()
        command = arguments[1] if isinstance(arguments[1], str) else " ".join(str(item) for item in arguments[1])
        if executable in {
            "open", "osascript", "chrome", "chrome.exe", "msedge", "msedge.exe",
            "google chrome", "microsoft edge", "safari",
        }:
            raise RuntimeError("Offline tests deny native browser and authorization dispatch.")
        if executable in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"} and "-verb runas" in command.casefold():
            raise RuntimeError("Offline tests deny native browser and authorization dispatch.")


sys.addaudithook(_deny_external_dispatch)
_DISPOSABLE_BROWSER_LAUNCH = ContextVar("disposable_browser_launch", default=False)


def _install_browser_guards() -> None:
    """Allow only explicitly requested clean browser fixtures, never profile attachment."""
    from playwright.async_api import BrowserType as AsyncBrowserType
    from playwright.async_api import APIRequestContext as AsyncAPIRequestContext
    from playwright.sync_api import APIRequestContext, Browser, BrowserType

    original_launch = BrowserType.launch
    original_context = Browser.new_context
    original_page = Browser.new_page

    def deny_profile_or_connection(*_args, **_kwargs):
        raise RuntimeError("Offline tests deny authenticated browser profiles and connections.")

    async def deny_async_dispatch(*_args, **_kwargs):
        raise RuntimeError("Offline tests deny asynchronous browser and API dispatch.")

    def deny_api_dispatch(*_args, **_kwargs):
        raise RuntimeError("Offline tests deny browser API dispatch; use an explicit fake response.")

    def guarded_launch(browser_type, **options):
        if not _DISPOSABLE_BROWSER_LAUNCH.get():
            raise RuntimeError("Real browser launch requires the disposable_browser_launch fixture.")
        if options.get("headless") is not True or set(options) - {"headless", "channel"}:
            raise RuntimeError("The disposable browser fixture requires a clean headless launch.")
        return original_launch(browser_type, **options)

    def isolate_context(context):
        def route_request(route):
            url = urlsplit(route.request.url)
            if url.scheme in {"about", "data", "blob"} or (
                _loopback_host(url.hostname) and url.port != 8666
            ):
                route.continue_()
            else:
                route.abort("blockedbyclient")

        context.route("**/*", route_request)
        return context

    def guarded_context(browser, **options):
        if {"storage_state", "http_credentials", "client_certificates", "proxy"}.intersection(options):
            raise RuntimeError("The disposable browser fixture cannot import authentication or proxy settings.")
        return isolate_context(original_context(browser, **{**options, "service_workers": "block"}))

    def guarded_page(browser, **options):
        if {"storage_state", "http_credentials", "client_certificates", "proxy"}.intersection(options):
            raise RuntimeError("The disposable browser fixture cannot import authentication or proxy settings.")
        page = original_page(browser, **{**options, "service_workers": "block"})
        isolate_context(page.context)
        return page

    BrowserType.launch = guarded_launch
    BrowserType.launch_persistent_context = deny_profile_or_connection
    BrowserType.connect = deny_profile_or_connection
    BrowserType.connect_over_cdp = deny_profile_or_connection
    Browser.new_context = guarded_context
    Browser.new_page = guarded_page
    for method in ("launch", "launch_persistent_context", "connect", "connect_over_cdp"):
        setattr(AsyncBrowserType, method, deny_async_dispatch)
    for method in ("get", "post", "put", "patch", "delete", "head", "fetch"):
        setattr(APIRequestContext, method, deny_api_dispatch)
        setattr(AsyncAPIRequestContext, method, deny_async_dispatch)


_install_browser_guards()

# Import web tooling only after host paths and real dispatch boundaries are isolated.
from flask import Flask  # noqa: E402
from flask.testing import FlaskClient  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    """Opt into failure details before a cancelled CI run loses the final summary."""
    if os.environ.get("AGENTIC_CONTEXT_TEST_REPORT_FAILURES") == "1":
        from tests.failure_reporter import ImmediateFailureReporter

        config.pluginmanager.register(
            ImmediateFailureReporter(config), "agenticcontext-immediate-failures"
        )


@pytest.fixture(scope="session")
def disposable_browser_launch():
    """Launch a clean browser for local fixtures without authorizing provider access."""
    def launch(browser_type, **options):
        token = _DISPOSABLE_BROWSER_LAUNCH.set(True)
        try:
            return browser_type.launch(**options)
        finally:
            _DISPOSABLE_BROWSER_LAUNCH.reset(token)

    return launch


def pytest_sessionfinish() -> None:
    """Remove the process-wide home-directory fixture after pytest exits."""
    # Windows cannot unlink the runtime log while its file handler is open.
    logging.shutdown()
    _TEST_SANDBOX.cleanup()


@pytest.fixture
def app() -> Flask:
    """Create a Flask application instance for route-level tests."""
    from app.core.config import LOCAL_STORE_ROOT
    from app.web.app import create_app

    application = create_app(LOCAL_STORE_ROOT)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    """Return the configured Flask test client."""
    return app.test_client()


@pytest.fixture
def macos_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose macOS-only browser descriptors to portable tests."""
    from app.core import browser_sessions, config

    monkeypatch.setattr(config, "is_macos_host", lambda: True)
    monkeypatch.setattr(browser_sessions, "is_macos_host", lambda: True)
