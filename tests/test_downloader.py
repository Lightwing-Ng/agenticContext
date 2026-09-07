"""Tests for yt-dlp output classification and retry boundaries.

Code version: v1.3.2-codex.1
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from app.core.config import CrawlConfig
from app.core.downloader import (
    MEDIA_MARKER_PREFIX,
    METADATA_MARKER_PREFIX,
    build_cookies_from_browser_arg,
    count_downloaded_media_types,
    discard_oversized_downloads,
    is_existing_file_conflict,
    is_missing_media_skip_output,
    is_max_file_size_skip_output,
    is_not_found_skip_output,
    is_successful_skip_output,
    is_suspended_skip_output,
    is_transient_retryable_output,
    is_unsupported_external_url_skip_output,
    parse_downloaded_paths,
    parse_download_metadata,
    metadata_for_downloaded_path,
    download_tweet_media,
    run_yt_dlp_with_retries,
)
from app.core.state import TaskState


def test_output_parsing_counts_media_and_classifies_skips() -> None:
    output = "\n".join(
        [
            "ordinary output",
            f"{MEDIA_MARKER_PREFIX}/tmp/photo.JPG",
            f"{MEDIA_MARKER_PREFIX}/tmp/video.mp4",
            f"{MEDIA_MARKER_PREFIX}/tmp/unknown.txt",
        ]
    )

    paths = parse_downloaded_paths(output)

    assert paths == [Path("/tmp/photo.JPG"), Path("/tmp/video.mp4"), Path("/tmp/unknown.txt")]
    assert count_downloaded_media_types(paths) == (1, 1)
    assert is_successful_skip_output("[download] file already exists")
    assert is_missing_media_skip_output("No video could be found in this tweet")
    assert is_not_found_skip_output("HTTP Error 404")
    assert is_suspended_skip_output("account: suspended")
    assert is_existing_file_conflict("unable to rename because the file exists")
    assert is_max_file_size_skip_output("File is larger than max-filesize")


def test_output_parsing_matches_metadata_without_json_sidecars(tmp_path) -> None:
    media_path = tmp_path / "demo" / "123" / "123.jpg"
    metadata = {
        "filepath": str(media_path),
        "display_id": "123",
        "title": "Cached post",
        "webpage_url": "https://x.com/demo/status/123",
    }
    output = f"{METADATA_MARKER_PREFIX}{json.dumps(metadata)}"

    rows = parse_download_metadata(output)

    assert rows == [metadata]
    assert metadata_for_downloaded_path(media_path, rows) == metadata


def test_discard_oversized_downloads_removes_only_files_above_limit(tmp_path) -> None:
    small_path = tmp_path / "small.jpg"
    large_path = tmp_path / "large.mp4"
    small_path.write_bytes(b"small")
    large_path.write_bytes(b"large-file")

    accepted, oversized = discard_oversized_downloads([small_path, large_path], max_file_size_bytes=5)

    assert accepted == [small_path]
    assert oversized == [large_path]
    assert small_path.exists()
    assert not large_path.exists()


def test_download_tweet_media_passes_the_universal_size_limit_to_yt_dlp(tmp_path) -> None:
    config = CrawlConfig(max_media_file_size_mib=1, x_browser="safari")
    state = TaskState("test")
    oversized_result = subprocess.CompletedProcess(
        ["yt-dlp"],
        1,
        stdout="",
        stderr="File is larger than max-filesize (2097152 bytes > 1048576 bytes).",
    )

    with patch("app.core.downloader.ensure_yt_dlp_available", return_value=["yt-dlp"]), patch(
        "app.core.downloader.build_cookies_from_browser_arg", return_value="safari"
    ), patch("app.core.downloader.run_yt_dlp_with_retries", return_value=oversized_result) as run_yt_dlp:
        result = download_tweet_media(
            "https://x.com/demo/status/1",
            tmp_path / "x",
            config,
            state,
        )

    command = run_yt_dlp.call_args.args[0]
    limit_index = command.index("--max-filesize")
    assert command[limit_index + 1] == str(1 * 1024 * 1024)
    assert "--write-info-json" not in command
    assert f"after_move:{METADATA_MARKER_PREFIX}%()j" in command
    assert result.skipped
    assert result.skipped_oversized_media_count == 1


def test_external_url_classifier_does_not_skip_native_x_urls() -> None:
    assert is_unsupported_external_url_skip_output("Unsupported URL: https://example.com/video")
    assert not is_unsupported_external_url_skip_output("Unsupported URL: https://x.com/user/status/1")


def test_browser_cookie_arguments_follow_selected_browser() -> None:
    chrome_argument = build_cookies_from_browser_arg(CrawlConfig(x_browser="chrome"))
    assert chrome_argument.startswith("chrome:")
    assert build_cookies_from_browser_arg(CrawlConfig(x_browser="safari")) == "safari"
    with pytest.raises(RuntimeError, match="Unsupported X browser"):
        build_cookies_from_browser_arg(CrawlConfig(x_browser="firefox"))


def test_safari_cookie_source_is_independent_of_host_browser_registry(monkeypatch) -> None:
    """Keep yt-dlp's Safari cookie source portable across CI host platforms."""
    monkeypatch.setattr("app.core.downloader.browser_descriptors", lambda _config: {})

    assert build_cookies_from_browser_arg(CrawlConfig(x_browser="safari")) == "safari"


def test_transient_yt_dlp_failure_retries_then_returns_success() -> None:
    transient = subprocess.CompletedProcess(["yt-dlp"], 1, stdout="", stderr="timed out")
    success = subprocess.CompletedProcess(["yt-dlp"], 0, stdout="done", stderr="")

    with patch("app.core.downloader.subprocess.run", side_effect=[transient, success]) as runner, patch(
        "app.core.downloader.time.sleep"
    ) as sleep:
        result = run_yt_dlp_with_retries(["yt-dlp"], "https://x.com/demo/status/1")

    assert result is success
    assert runner.call_count == 2
    sleep.assert_called_once()


def test_non_transient_yt_dlp_failure_does_not_retry() -> None:
    failure = subprocess.CompletedProcess(["yt-dlp"], 1, stdout="", stderr="HTTP Error 404")

    with patch("app.core.downloader.subprocess.run", return_value=failure) as runner:
        result = run_yt_dlp_with_retries(["yt-dlp"], "https://x.com/demo/status/1")

    assert result is failure
    assert runner.call_count == 1
    assert not is_transient_retryable_output(failure.stderr)
