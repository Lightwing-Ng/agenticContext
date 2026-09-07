"""Service orchestration and Flask contract tests.

Code version: v1.8.4-codex.1
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.config import MAX_DOWNLOAD_WORKERS, CrawlConfig
from app.core.agent import browser_profile_cache_identity
from app.core.downloader import DownloadResult
from app.core.grok_downloader import GrokResetResult
from app.core.grok_service import summarize_error_for_status
from app.core.service import CacheLikesService
from app.core.state import TaskSnapshot, TaskState
from app.web.app import create_app
from app.web.cache_sources import CACHE_SOURCE_VIEWS


def test_cache_service_run_aggregates_download_results_without_browser_access(tmp_path: Path) -> None:
    state = TaskState("v-test", snapshot_factory=lambda version: TaskSnapshot(version=version))
    service = CacheLikesService(state)
    config = CrawlConfig(download_workers=1, max_media_items=10, account_name_override="saved")
    results = [
        DownloadResult(downloaded_media_count=2, downloaded_post_count=1, downloaded_image_count=1, downloaded_video_count=1),
        DownloadResult(skipped=True),
    ]

    with patch("app.core.service.LOCAL_STORE_ROOT", tmp_path), patch(
        "app.core.service.collect_liked_tweet_urls",
        return_value=("detected", ["https://x.com/demo/status/1", "https://x.com/demo/status/2"]),
    ), patch("app.core.service.LocalTweetCacheIndex.build"), patch(
        "app.core.service.download_tweet_media",
        side_effect=results,
    ):
        state.reset_for_run()
        service._run(config)

    snapshot = state.snapshot()
    assert snapshot["phase"] == "finished"
    assert snapshot["downloaded_posts"] == 1
    assert snapshot["downloaded_images"] == 1
    assert snapshot["downloaded_videos"] == 1
    assert snapshot["skipped_tweets"] == 1
    assert snapshot["output_dir"] == str(tmp_path / "x")


def test_grok_status_error_summary_is_actionable_and_bounded() -> None:
    launch_error = RuntimeError("BrowserType.launch_persistent_context: profile locked")
    assert "browser profile" in summarize_error_for_status(launch_error)
    assert summarize_error_for_status(RuntimeError("first line\nsecond line")) == "first line"
    assert summarize_error_for_status(RuntimeError("x" * 600)).endswith("...")


@pytest.mark.integration
def test_web_pages_and_status_apis_are_available(client) -> None:
    for path, expected_text in (
            ("/cache/x", b"Execution overview"),
            ("/cache/grok", b"Grok library overview"),
            ("/cache/chatgpt", b"ChatGPT cache overview"),
            ("/cache/gemini", b"Gemini history cache overview"),
            ("/cache/claude", b"Claude history cache overview"),
        ("/settings", b"Configuration center"),
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert expected_text in response.data

    status = client.get("/api/status")
    grok_status = client.get("/api/grok/status")
    chatgpt_status = client.get("/api/chatgpt/status")
    gemini_status = client.get("/api/gemini/status")
    claude_status = client.get("/api/claude/status")
    generic_statuses = [client.get(f"/api/cache/{source.key}/status") for source in CACHE_SOURCE_VIEWS]
    assert status.status_code == 200
    assert grok_status.status_code == 200
    assert chatgpt_status.status_code == 200
    assert gemini_status.status_code == 200
    assert claude_status.status_code == 200
    assert all(response.status_code == 200 for response in generic_statuses)
    assert status.get_json()["phase"] in {
        "idle",
        "starting",
        "collecting",
        "downloading",
        "paused",
        "finished",
        "failed",
        "stopped",
        "stopping",
    }


@pytest.mark.integration
def test_legacy_cache_page_paths_redirect_to_canonical_namespace(client) -> None:
    for legacy_path, canonical_path in (
        ("/", "/cache/x"),
        ("/grok", "/cache/grok"),
        ("/chatgpt", "/cache/chatgpt"),
        ("/gemini", "/cache/gemini"),
        ("/claude", "/cache/claude"),
    ):
        response = client.get(legacy_path)
        assert response.status_code == 302
        assert response.headers["Location"] == canonical_path

    query_response = client.get("/chatgpt?agent_platform=gemini")
    assert query_response.status_code == 302
    assert query_response.headers["Location"] == "/cache/chatgpt?agent_platform=gemini"


def test_browser_empty_cache_isolated_from_repository_cache(tmp_path: Path) -> None:
    application = create_app(tmp_path / "local_store")
    application.config.update(TESTING=True)

    with application.test_client() as isolated_client:
        response = isolated_client.get("/browser?view=media")

    assert response.status_code == 200
    assert b"Cached media browser" in response.data
    assert b"No cached media found." in response.data


@pytest.mark.integration
def test_browser_session_api_validates_inputs_and_returns_probe_payload(client) -> None:
    invalid = client.get("/api/browser-session?platform=unknown&browser=chrome")
    assert invalid.status_code == 400
    assert "Unsupported platform" in invalid.get_json()["error"]

    with patch("app.web.app.probe_browser_session", return_value={"ready": True, "account_name": "demo"}) as probe:
        valid = client.get("/api/browser-session?platform=x&browser=chrome")

    assert valid.status_code == 200
    assert valid.get_json() == {
        "ready": True, "account_name": "demo",
        "profile_identity": browser_profile_cache_identity("chrome", probe.call_args.args[2]),
    }
    probe.assert_called_once()


@pytest.mark.integration
def test_settings_and_grok_reset_routes_redirect_without_external_work(client, tmp_path: Path) -> None:
    with patch("app.web.app.save_config") as save_config:
        settings_response = client.post(
            "/settings",
            data={
                "download_workers": "1,234",
                "scroll_pause_seconds": "2.5",
                "chatgpt_startup_timeout_seconds": "45",
                "chatgpt_scan_wait_seconds": "0.25",
            },
        )
    assert settings_response.status_code == 302
    save_config.assert_called_once()
    saved_config = save_config.call_args.args[0]
    assert saved_config.download_workers == MAX_DOWNLOAD_WORKERS
    assert saved_config.chatgpt_startup_timeout_seconds == 45.0
    assert saved_config.chatgpt_scan_wait_seconds == 0.25

    with patch("app.web.app.reset_grok_state", return_value=GrokResetResult(removed_media_files=2, removed_state_files=1)):
        reset_response = client.post("/grok/reset")
    assert reset_response.status_code == 302
    assert client.get("/api/grok/status").status_code == 200


@pytest.mark.integration
def test_grok_start_route_accepts_safari(client, macos_host) -> None:
    # Keep the simulated macOS selection out of later tests' host settings.
    with (
        patch("app.core.grok_service.GrokDownloadService.start") as start,
        patch("app.web.app.save_config") as save_config,
    ):
        response = client.post(
            "/grok/start",
            data={
                "grok_browser": "safari",
                "download_workers": "2",
                "max_media_file_size_mib": "75",
            },
        )

    assert response.status_code == 302
    config = start.call_args.args[0]
    save_config.assert_called_once_with(config)
    assert config.grok_browser == "safari"
    assert config.download_workers == 2
    assert config.max_media_file_size_mib == 75


@pytest.mark.integration
def test_gemini_start_route_accepts_browser_and_history_limits(client) -> None:
    with patch("app.core.gemini_service.GeminiHistoryService.start") as start:
        response = client.post(
            "/cache/gemini/start",
            data={
                "gemini_browser": "edge",
                "gemini_max_conversations": "2,000",
                "gemini_scroll_pause_seconds": "0.35",
                "gemini_stale_round_limit": "7",
            },
        )

    assert response.status_code == 302
    config = start.call_args.args[0]
    assert config.gemini_browser == "edge"
    assert config.gemini_max_conversations == 2_000
    assert config.gemini_scroll_pause_seconds == 0.35
    assert config.gemini_stale_round_limit == 7


def test_chatgpt_text_start_preserves_media_settings_and_selects_text_mode(
    tmp_path: Path, macos_host
) -> None:
    saved_project_url = "https://chatgpt.com/c/specific-session"
    initial_config = CrawlConfig(chatgpt_project_url=saved_project_url)

    with patch("app.web.app.load_saved_config", return_value=initial_config), patch(
        "app.web.app.save_config"
    ) as save_config, patch("app.core.chatgpt_service.ChatGPTDownloadService.start") as start:
        application = create_app(tmp_path / "local_store")
        application.config.update(TESTING=True)
        with application.test_client() as isolated_client:
            response = isolated_client.post(
                "/cache/chatgpt/start",
                data={
                    "chatgpt_browser": "safari",
                    "chatgpt_content_mode": "text",
                },
            )

    assert response.status_code == 302
    runtime_config = start.call_args.args[0]
    assert runtime_config.chatgpt_browser == "safari"
    assert runtime_config.chatgpt_project_url == saved_project_url
    assert start.call_args.kwargs["content_mode"] == "text"
    assert save_config.call_args.args[0].chatgpt_project_url == saved_project_url


def test_chatgpt_media_start_uses_safari_project_settings(tmp_path: Path, macos_host) -> None:
    project_url = "https://chatgpt.com/g/g-p-demo/project"
    project_name = "Demo project"
    initial_config = CrawlConfig(chatgpt_project_url=project_url, chatgpt_project_name=project_name)

    with patch("app.web.app.load_saved_config", return_value=initial_config), patch(
        "app.web.app.save_config"
    ), patch("app.core.chatgpt_service.ChatGPTDownloadService.start") as start:
        application = create_app(tmp_path / "local_store")
        application.config.update(TESTING=True)
        with application.test_client() as isolated_client:
            response = isolated_client.post(
                "/cache/chatgpt/start",
                data={
                    "chatgpt_browser": "safari",
                    "chatgpt_content_mode": "media",
                    "chatgpt_project_url": project_url,
                    "chatgpt_project_name": project_name,
                },
            )

    assert response.status_code == 302
    runtime_config = start.call_args.args[0]
    assert runtime_config.chatgpt_browser == "safari"
    assert runtime_config.chatgpt_project_url == project_url
    assert runtime_config.chatgpt_project_name == project_name
    assert start.call_args.kwargs["content_mode"] == "media"


def test_chatgpt_media_start_passes_blank_url_for_all_generated_media(
    tmp_path: Path, macos_host
) -> None:
    saved_project_url = "https://chatgpt.com/g/g-p-saved/project"
    initial_config = CrawlConfig(chatgpt_project_url=saved_project_url)

    with patch("app.web.app.load_saved_config", return_value=initial_config), patch(
        "app.web.app.save_config"
    ) as save_config, patch("app.core.chatgpt_service.ChatGPTDownloadService.start") as start:
        application = create_app(tmp_path / "local_store")
        application.config.update(TESTING=True)
        with application.test_client() as isolated_client:
            response = isolated_client.post(
                "/cache/chatgpt/start",
                data={
                    "chatgpt_browser": "safari",
                    "chatgpt_content_mode": "media",
                    "chatgpt_project_url": "",
                },
            )

    assert response.status_code == 302
    runtime_config = start.call_args.args[0]
    assert runtime_config.chatgpt_project_url == ""
    assert save_config.call_args.args[0].chatgpt_project_url == ""
    assert start.call_args.kwargs["content_mode"] == "media"


@pytest.mark.integration
def test_partial_cache_start_preserves_unsubmitted_global_booleans(tmp_path: Path) -> None:
    initial_config = CrawlConfig(
        headless=True,
        shadow_backup_enabled=True,
        shadow_backup_auto_sync=True,
        shadow_backup_mirror_deletions=True,
    )

    with patch("app.web.app.load_saved_config", return_value=initial_config), patch(
        "app.web.app.save_config"
    ) as save_config, patch("app.core.service.CacheLikesService.start") as start:
        application = create_app(tmp_path / "local_store")
        application.config.update(TESTING=True)
        with application.test_client() as isolated_client:
            response = isolated_client.post(
                "/start",
                data={
                    "x_browser": "safari",
                    "download_workers": "3",
                    "max_media_file_size_mib": "50",
                },
            )

    assert response.status_code == 302
    config = start.call_args.args[0]
    assert config.headless
    assert config.shadow_backup_enabled
    assert config.shadow_backup_auto_sync
    assert config.shadow_backup_mirror_deletions
    save_config.assert_called_once_with(config)
