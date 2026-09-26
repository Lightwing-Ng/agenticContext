"""Cache route admission, mode dispatch, and X counter isolation.

Code version: v1.0.0-codex.0
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.x_text_history import XTextHistoryStore, XTextPost, x_text_history_path
from app.web.app import create_app


@pytest.mark.parametrize("source", ("chatgpt", "x", "grok", "claude"))
@pytest.mark.parametrize("mode", ("text", "media"))
def test_safari_cache_modes_preserve_selection_and_dispatch(tmp_path: Path, macos_host, source: str, mode: str) -> None:
    application = create_app(tmp_path / "local_store")
    client = application.test_client()
    response = client.get(f"/cache/{source}/{mode}/safari")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'data-cache-browser="safari"' in body
    for target in ("chatgpt", "x", "grok", "claude"):
        assert f'data-cache-source-switcher-path="/cache/{target}/{mode}/safari"' in body
    targets = {
        "chatgpt": "app.core.chatgpt_service.ChatGPTDownloadService.start",
        "x": "app.core.service.CacheLikesService.start",
        "claude": "app.core.claude_history_service.ClaudeHistoryService.start",
        "grok": (
            "app.core.grok_history_service.GrokHistoryService.start" if mode == "text"
            else "app.core.grok_service.GrokDownloadService.start"
        ),
    }
    with patch(targets[source]) as start, patch("app.web.config_store.save_config"):
        started = client.post(
            f"/cache/{source}/start",
            data={"cache_content_mode": mode, f"{source}_browser": "safari"},
        )
    assert started.location == f"/cache/{source}/{mode}/safari"
    start.assert_called_once()
    assert getattr(start.call_args.args[0], f"{source}_browser") == "safari"
    assert start.call_args.kwargs == ({} if source == "grok" else {"content_mode": mode})


def test_x_text_status_reads_only_text_cache(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    XTextHistoryStore(x_text_history_path(root)).upsert([
        XTextPost("https://x.com/example/status/123", "A text-only liked post."),
    ])
    application = create_app(root)
    with patch("app.web.app.build_initial_snapshot", side_effect=AssertionError("Text read media")):
        status = application.test_client().get("/api/cache/x/status?content_mode=text").get_json()
    assert status["cached_text_posts"] == 1
    assert status["downloaded_images"] == 0
    assert status["downloaded_posts"] == 0
    assert status["output_dir"] == str(root / "llm" / "x")
