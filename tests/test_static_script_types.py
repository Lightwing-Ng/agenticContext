"""Keep bundled JavaScript importable despite host MIME registry overrides.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

import pytest
from playwright.sync_api import Browser

from app.web.app import create_app
from tests import test_sidebar_e2e as sidebar


disposable_browser = sidebar.disposable_browser
sidebar_server_url = sidebar.sidebar_server_url


@pytest.fixture
def wrong_script_types(monkeypatch):
    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")
    monkeypatch.setitem(mimetypes.types_map, ".mjs", "application/octet-stream")
    assert mimetypes.guess_type("script.js")[0] == "text/plain"
    assert mimetypes.guess_type("module.mjs")[0] == "application/octet-stream"


@pytest.fixture
def script_client(tmp_path: Path, wrong_script_types):
    application = create_app(
        tmp_path / "store", agent_external_operations_enabled=False
    )
    application.config.update(TESTING=True)
    try:
        with application.test_client() as client:
            yield client
    finally:
        application.extensions["runtime_shutdown"]()


@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("asset", ["sidebar.js", "vendor/fuse.min.mjs", "beta/engines.mjs"])
def test_bundled_scripts_use_canonical_javascript_type(script_client, method, asset):
    response = getattr(script_client, method)(f"/static/{asset}")
    assert response.status_code == 200
    assert response.mimetype == "text/javascript"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert bool(response.data) is (method == "get")


def test_script_ranges_keep_their_partial_content_and_javascript_type(script_client):
    response = script_client.get("/static/sidebar.js", headers={"Range": "bytes=0-15"})
    assert response.status_code == 206
    assert response.mimetype == "text/javascript"
    assert len(response.data) == 16


def test_static_errors_and_other_response_types_keep_their_original_mime(script_client):
    missing = script_client.get("/static/absent-module.mjs")
    assert missing.status_code == 404
    assert missing.mimetype == "text/html"
    assert script_client.get("/static/style.css").mimetype == "text/css"
    assert script_client.get("/beta?example=module.mjs").mimetype == "text/html"


def test_native_browser_imports_modules_with_poisoned_host_types(
    disposable_browser: Browser, sidebar_server_url: str, wrong_script_types
):
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.goto(sidebar_server_url + "/beta/idea-collision")
        imported = page.evaluate("""async () => {
            const fuse = await import('/static/vendor/fuse.min.mjs');
            const engines = await import('/static/beta/engines.mjs');
            return {fuse: typeof fuse.default, engines: typeof engines.runExperiment};
        }""")
        assert imported["fuse"] == "function"
        assert imported["engines"] == "function"
    finally:
        context.close()
