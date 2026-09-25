"""Tests for yt-dlp output classification and retry boundaries.

Code version: v1.4.0-codex.0
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.cache_catalog import LocalTweetCacheIndex
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

    assert [str(path) for path in paths] == ["/tmp/photo.JPG", "/tmp/video.mp4", "/tmp/unknown.txt"]
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


def test_transient_yt_dlp_failure_retries_then_returns_success(tmp_path: Path, monkeypatch) -> None:
    attempts_path = tmp_path / "attempts.txt"
    script_path = tmp_path / "fake_yt_dlp.py"
    script_path.write_text(
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "count = int(path.read_text()) + 1 if path.exists() else 1\n"
        "path.write_text(str(count))\n"
        "if count == 1:\n"
        "    print('timed out', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print('done')\n"
    )
    monkeypatch.setattr("app.core.downloader.DOWNLOAD_RETRY_DELAY_SECONDS", 0.01)

    result = run_yt_dlp_with_retries(
        [sys.executable, str(script_path), str(attempts_path)],
        "https://x.com/demo/status/1",
        timeout_seconds=10,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "done"
    assert attempts_path.read_text() == "2"


def test_non_transient_yt_dlp_failure_does_not_retry(tmp_path: Path) -> None:
    attempts_path = tmp_path / "attempts.txt"
    script_path = tmp_path / "fake_yt_dlp.py"
    script_path.write_text(
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "count = int(path.read_text()) + 1 if path.exists() else 1\n"
        "path.write_text(str(count))\n"
        "print('HTTP Error 404', file=sys.stderr)\n"
        "sys.exit(1)\n"
    )

    result = run_yt_dlp_with_retries(
        [sys.executable, str(script_path), str(attempts_path)],
        "https://x.com/demo/status/1",
        timeout_seconds=10,
    )

    assert result.returncode == 1
    assert attempts_path.read_text() == "1"
    assert not is_transient_retryable_output(result.stderr)


@pytest.mark.parametrize("reason", ["stopped", "timed_out"])
def test_interrupted_yt_dlp_keeps_completed_media_and_existing_files(
    tmp_path: Path,
    reason: str,
) -> None:
    output_dir = tmp_path / "x"
    prior_path = output_dir / "prior" / "999" / "999.jpg"
    prior_path.parent.mkdir(parents=True)
    prior_path.write_bytes(b"existing cached image")
    ready_path = tmp_path / "ready.pid"
    script_path = tmp_path / "fake_yt_dlp.py"
    script_path.write_text(
        "import os, pathlib, sys, time\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "ready = pathlib.Path(sys.argv[2])\n"
        "media = root / 'poster' / '123' / '123.jpg'\n"
        "media.parent.mkdir(parents=True, exist_ok=True)\n"
        "media.write_bytes(b'completed image')\n"
        "(media.parent / 'next.part').write_bytes(b'partial')\n"
        "print('__CACHELIKES_MEDIA__:' + str(media), flush=True)\n"
        "ready.write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    state = TaskState("test")
    config = CrawlConfig(x_browser="safari", max_media_file_size_mib=1)
    with patch(
        "app.core.downloader.ensure_yt_dlp_available",
        return_value=[sys.executable, str(script_path), str(output_dir), str(ready_path)],
    ):
        started = time.monotonic()
        result = download_tweet_media(
            "https://x.com/poster/status/123",
            output_dir,
            config,
            state,
            should_stop=(lambda: ready_path.exists()) if reason == "stopped" else None,
            timeout_seconds=10 if reason == "stopped" else 0.5,
        )
        elapsed = time.monotonic() - started

    assert elapsed < 5
    assert result.stopped is (reason == "stopped")
    assert result.timed_out is (reason == "timed_out")
    assert result.downloaded_media_count == 1
    assert result.downloaded_image_count == 1
    assert prior_path.read_bytes() == b"existing cached image"
    assert (output_dir / "poster" / "123" / "123.jpg").read_bytes() == b"completed image"
    assert (output_dir / "poster" / "123" / "next.part").read_bytes() == b"partial"
    assert LocalTweetCacheIndex.build(output_dir).contains_complete_cache(
        "https://x.com/poster/status/123"
    )
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(int(ready_path.read_text()), 0)
