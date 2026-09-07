"""Regression checks for the default offline test boundary.

Code version: v1.1.0-codex.1
"""

import asyncio
import ntpath
import os
from pathlib import Path
import sys
import tempfile

import pytest
from playwright.async_api import BrowserType as AsyncBrowserType
from playwright.sync_api import APIRequestContext, Browser, BrowserType


def test_windows_and_posix_paths_share_the_private_test_home(monkeypatch):
    from app.core import config

    home = Path(os.environ["HOME"])
    assert home == Path(os.environ["USERPROFILE"])
    assert ntpath.expanduser("~") == str(home)
    assert os.environ["HOMEDRIVE"] + os.environ["HOMEPATH"] == str(home)
    for variable in ("LOCALAPPDATA", "APPDATA"):
        assert Path(os.environ[variable]).is_relative_to(home)
    for variable in ("TMPDIR", "TEMP", "TMP"):
        assert Path(os.environ[variable]) == Path(tempfile.gettempdir())
        assert Path(os.environ[variable]).parent == home.parent
    monkeypatch.setattr(config, "is_windows_host", lambda: True)
    assert config.default_edge_user_data_dir().is_relative_to(home)
    assert config.default_chrome_user_data_dir().is_relative_to(home)
    assert config.resolve_runtime_root().parent == home.parent


@pytest.mark.parametrize("method", ("launch", "launch_persistent_context", "connect", "connect_over_cdp"))
def test_real_browser_dispatch_requires_an_explicit_clean_fixture(method):
    with pytest.raises(RuntimeError, match="(disposable_browser_launch|authenticated browser)"):
        getattr(BrowserType, method)(None)


@pytest.mark.parametrize("event,arguments", (
    ("socket.getaddrinfo", ("provider.example", 443, 0, 0, 0)),
    ("socket.connect", (None, ("192.0.2.1", 443))),
    ("socket.connect", (None, ("127.0.0.1", 8666))),
    ("subprocess.Popen", ("/usr/bin/open", ["open", "https://provider.example"], None, None)),
    ("subprocess.Popen", (r"C:\Browser\msedge.exe", ["msedge.exe"], None, None)),
))
def test_dispatch_audit_guard_rejects_external_boundaries_without_executing_them(event, arguments):
    with pytest.raises(RuntimeError, match="Offline tests deny"):
        sys.audit(event, *arguments)


def test_clean_browser_fixture_still_rejects_profile_or_headed_options(disposable_browser_launch):
    with pytest.raises(RuntimeError, match="clean headless launch"):
        disposable_browser_launch(object.__new__(BrowserType), headless=False)


def test_async_browser_and_unrouted_api_requests_are_denied():
    with pytest.raises(RuntimeError, match="asynchronous browser"):
        asyncio.run(AsyncBrowserType.launch(None))
    with pytest.raises(RuntimeError, match="browser API dispatch"):
        APIRequestContext.get(None, "https://provider.example")


@pytest.mark.parametrize("method", ("new_context", "new_page"))
def test_clean_browser_cannot_import_saved_authentication(method):
    with pytest.raises(RuntimeError, match="cannot import authentication"):
        getattr(Browser, method)(None, storage_state="private-user-state.json")
