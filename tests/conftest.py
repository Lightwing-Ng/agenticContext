"""Shared pytest fixtures for isolated agenticContext tests.

Code version: v1.2.3-codex.1
"""

from __future__ import annotations

import os
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from flask import Flask
from flask.testing import FlaskClient


_TEST_HOME = TemporaryDirectory(prefix="cachelikes-pytest-home-")
_TEST_RUNTIME_ROOT = TemporaryDirectory(prefix="cachelikes-pytest-runtime-")
# Keep attachment/editor duplicate copies out of test collection without deleting user files.
collect_ignore_glob = ["* 2.py"]
os.environ["HOME"] = _TEST_HOME.name
os.environ["AGENTIC_CONTEXT_RUNTIME_ROOT"] = _TEST_RUNTIME_ROOT.name
os.environ["AGENTIC_CONTEXT_SETTINGS_PATH"] = str(
    Path(_TEST_RUNTIME_ROOT.name) / "settings" / "settings.json"
)


def pytest_sessionfinish() -> None:
    """Remove the process-wide home-directory fixture after pytest exits."""
    # Windows cannot unlink the runtime log while its file handler is open.
    logging.shutdown()
    _TEST_RUNTIME_ROOT.cleanup()
    _TEST_HOME.cleanup()


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
