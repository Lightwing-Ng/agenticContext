"""Grok Build local Cache routing and browser interaction contracts.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest


def test_local_cache_has_folder_control_without_browser_or_media(client):
    response = client.get("/cache/grok_build?content_mode=media")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'aria-label="Cache source: Grok Build"' in html
    assert 'name="grok_build_directory"' in html
    assert "Read local sessions" in html
    assert 'data-cache-page-content-mode="text"' in html
    assert 'data-cache-local="true"' in html
    assert "data-browser-session-panel" not in html
    assert "data-cache-content-mode-option" not in html
    assert "source=grok_build" in html
    assert client.get("/cache/grok_build/media/edge").status_code == 404


def test_start_local_cache_preserves_browser_settings(client, app, tmp_path):
    service = app.extensions["cache_runtimes"]["grok_build"]["text"].service
    service.start = Mock()
    folder = tmp_path / "Grok Build"
    folder.mkdir()
    response = client.post("/cache/grok_build/start", data={
        "grok_build_directory": str(folder),
        "cache_content_mode": "media",
    })
    assert response.status_code == 302
    assert response.location == "/cache/grok_build"
    config = service.start.call_args.args[0]
    assert config.grok_build_directory == folder
    assert service.start.call_args.kwargs == {"content_mode": "text"}


def test_directory_picker_and_settings_accept_code_source(client, tmp_path):
    folder = tmp_path / "Grok Build"
    folder.mkdir()
    response = client.post("/api/settings/directory", json={
        "field": "grok_build_directory", "path": str(folder),
    })
    assert response.status_code == 200
    assert Path(response.get_json()["current_path"]) == folder.resolve()
    html = client.get("/settings").get_data(as_text=True)
    assert "Grok Chat · Text" in html
    assert "Grok Build · Local" in html
    assert 'name="grok_build_directory"' in html


@pytest.mark.parametrize("source", ["claude", "claude_code", "chatgpt", "grok", "gemini", "x", "zhihu"])
def test_existing_sources_offer_separate_chat_and_code(client, source):
    html = client.get(f"/cache/{source}").get_data(as_text=True)
    assert 'data-cache-source-switcher-option="grok"' in html
    assert 'data-cache-source-switcher-option="grok_build"' in html
    assert 'data-cache-source-switcher-path="/cache/grok_build"' in html
    assert "Grok Chat" in html
