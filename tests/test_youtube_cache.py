"""Cover YouTube input validation, cache publication, and local HTTP access.

Code version: v1.4.2-codex.0
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
import os
from pathlib import Path
import subprocess
from threading import Event
from time import monotonic
from unittest.mock import Mock

import pytest
from flask.testing import FlaskClient
from werkzeug.http import parse_options_header

from app.core.agent_access_security import AGENT_ACCESS_SESSION_KEY
from app.core.youtube_cache import (
    YouTubeCacheError,
    YouTubeCacheResult,
    _downloader_runtime,
    download_youtube_media,
    parse_youtube_video_id,
    read_cached_youtube_media,
    youtube_cache_path,
)
from app.web.app import create_app


VIDEO_ID = "eLDdNjQNSY8"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
DOWNLOAD_ENDPOINT = "/api/cache/youtube/download"
RECENT_ENDPOINT = "/api/cache/youtube/recent"
MEDIA_BYTES = b"a completed download fixture"


def test_provisioned_youtube_runtime_keeps_default_and_fallback_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_name = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    python = tmp_path / "venv" / python_name
    python.parent.mkdir(parents=True)
    python.touch()
    (tmp_path / "provider" / "server" / "build").mkdir(parents=True)
    monkeypatch.setenv("AGENTIC_CONTEXT_YOUTUBE_RUNTIME", str(tmp_path))
    monkeypatch.setattr("app.core.youtube_cache.shutil.which", lambda name: f"/test-tools/{name}")

    command, arguments, _ = _downloader_runtime()

    assert command == [str(python), "-m", "yt_dlp"]
    assert "youtube:player_client=default,mweb,tv,web_safari,web_embedded" in arguments


@pytest.fixture
def youtube_client(tmp_path: Path) -> Iterator[FlaskClient]:
    """Serve YouTube routes from a disposable local store."""
    application = create_app(tmp_path / "local-store")
    application.config.update(TESTING=True)
    with application.test_client() as client:
        yield client


@pytest.mark.parametrize(
    "url",
    (
        f"{VIDEO_URL}&list=RD{VIDEO_ID}&start_radio=1",
        f"https://youtu.be/{VIDEO_ID}?si=share",
        f"https://m.youtube.com/shorts/{VIDEO_ID}",
    ),
)
def test_youtube_links_select_one_video(url: str) -> None:
    assert parse_youtube_video_id(url) == VIDEO_ID


@pytest.mark.parametrize(
    "url",
    (
        f"https://youtube.com.invalid/watch?v={VIDEO_ID}",
        f"https://youtube.com@127.0.0.1/watch?v={VIDEO_ID}",
        f"https://name:password@www.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com:9000/watch?v={VIDEO_ID}",
        f"file://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/playlist?list=RD{VIDEO_ID}",
        f"{VIDEO_URL}&v=12345678901",
        "https://youtu.be/bad-id",
    ),
)
def test_youtube_links_reject_other_targets(url: str) -> None:
    with pytest.raises(ValueError):
        parse_youtube_video_id(url)


@pytest.mark.parametrize(("mode", "extension"), (("video", "mp4"), ("audio", "m4a")))
def test_first_download_publishes_once_and_then_uses_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    extension: str,
) -> None:
    target = tmp_path / "media" / "youtube" / VIDEO_ID / f"youtube_{VIDEO_ID}.{extension}"

    def finish_download(command: list[str], _url: str, **_kwargs) -> subprocess.CompletedProcess:
        assert command[-1] == VIDEO_URL
        assert "--no-playlist" in command
        assert "--no-mtime" in command
        assert "--cookies-from-browser" not in command
        output = command[command.index("--output") + 1]
        stage = Path(output.replace("%(ext)s", extension))
        assert stage != target
        assert not target.exists()
        stage.write_bytes(MEDIA_BYTES)
        metadata = Path(command[command.index("--print-to-file") + 2])
        assert metadata.parent == stage.parent
        metadata.write_text(
            json.dumps({"title": "现场 / Live: Take 1", "uploader": "Studio / Sessions", "channel": "Studio"}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    worker = Mock(side_effect=finish_download)
    verify = Mock()
    monkeypatch.setattr("app.core.youtube_cache.shutil.which", lambda name: f"/test-tools/{name}")
    monkeypatch.setattr("app.core.youtube_cache._downloader_runtime", lambda: (["yt-dlp"], [], None))
    monkeypatch.setattr("app.core.youtube_cache._verify_download", verify)
    monkeypatch.setattr("app.core.youtube_cache.run_yt_dlp_with_retries", worker)
    result = download_youtube_media(tmp_path, VIDEO_ID, mode, 1_000)
    reused = download_youtube_media(tmp_path, VIDEO_ID, mode, 1_000)

    assert result.path == reused.path == target
    assert result.filename == reused.filename == f"YouTube {mode.title()} · 现场 Live Take 1 · Studio Sessions.{extension}"
    assert target.read_bytes() == MEDIA_BYTES
    assert result.size_bytes == reused.size_bytes == len(MEDIA_BYTES)
    assert not result.cached
    assert reused.cached
    worker.assert_called_once()
    verify.assert_called_once()
    assert not verify.call_args.args[1].parent.exists()


def test_oversized_download_leaves_no_cached_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def download_oversized(command: list[str], _url: str, **_kwargs) -> subprocess.CompletedProcess:
        output = command[command.index("--output") + 1]
        Path(output.replace("%(ext)s", "mp4")).write_bytes(MEDIA_BYTES)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    verify = Mock()
    monkeypatch.setattr("app.core.youtube_cache.shutil.which", lambda name: f"/test-tools/{name}")
    monkeypatch.setattr("app.core.youtube_cache._downloader_runtime", lambda: (["yt-dlp"], [], None))
    monkeypatch.setattr("app.core.youtube_cache._verify_download", verify)
    monkeypatch.setattr("app.core.youtube_cache.run_yt_dlp_with_retries", download_oversized)

    with pytest.raises(YouTubeCacheError, match="maximum file size"):
        download_youtube_media(tmp_path, VIDEO_ID, "video", len(MEDIA_BYTES) - 1)

    destination = youtube_cache_path(tmp_path, VIDEO_ID, "video")
    assert not destination.exists()
    assert not tuple(destination.parent.iterdir())
    verify.assert_not_called()


def test_youtube_mutations_require_a_valid_input_from_loopback(
    youtube_client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = Mock(side_effect=AssertionError("A rejected request started a worker."))
    monkeypatch.setattr("app.web.youtube_cache_routes.download_youtube_media", worker)
    denied_input = youtube_client.post(DOWNLOAD_ENDPOINT, json={"url": "https://example.test/", "mode": "video"})
    assert denied_input.status_code == 400
    denied_mode = youtube_client.post(DOWNLOAD_ENDPOINT, json={"url": VIDEO_URL, "mode": "playlist"})
    assert denied_mode.status_code == 400

    lan_url = "http://192.168.1.30:8666"
    with youtube_client.session_transaction(base_url=lan_url) as session:
        session[AGENT_ACCESS_SESSION_KEY] = True
    denied_lan = youtube_client.post(
        DOWNLOAD_ENDPOINT,
        base_url=lan_url,
        headers={"Origin": lan_url},
        environ_overrides={"REMOTE_ADDR": "192.168.1.31"},
        json={"url": VIDEO_URL, "mode": "video"},
    )
    assert denied_lan.status_code == 403
    worker.assert_not_called()


def test_cached_youtube_video_stays_local_on_both_download_routes(
    youtube_client: FlaskClient,
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-store"
    video = youtube_cache_path(root, VIDEO_ID, "video")
    video.parent.mkdir(parents=True)
    video.write_bytes(MEDIA_BYTES)
    cached = read_cached_youtube_media(video)
    assert cached is not None and cached.filename == f"YouTube Video · {VIDEO_ID}.mp4"
    paths = (
        f"/api/cache/youtube/media/{VIDEO_ID}/video",
        f"/browser/media/{video.relative_to(root.resolve()).as_posix()}",
    )
    lan_url = "http://192.168.1.30:8666"
    with youtube_client.session_transaction(base_url=lan_url) as session:
        session[AGENT_ACCESS_SESSION_KEY] = True

    for path in paths:
        local = youtube_client.get(path, buffered=True)
        assert local.status_code == 200
        assert local.data == MEDIA_BYTES
        lan = youtube_client.get(
            path,
            base_url=lan_url,
            environ_overrides={"REMOTE_ADDR": "192.168.1.31"},
        )
        assert lan.status_code == 403


def test_recent_youtube_media_find_existing_audio_and_video_after_restart(
    youtube_client: FlaskClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = Mock(side_effect=AssertionError("Listing recent files started a download."))
    monkeypatch.setattr("app.core.youtube_cache.run_yt_dlp_with_retries", worker)
    assert youtube_client.get(RECENT_ENDPOINT).get_json() == {"items": []}

    root = tmp_path / "local-store"
    audio = youtube_cache_path(root, VIDEO_ID, "audio")
    audio.parent.mkdir(parents=True)
    audio.write_bytes(MEDIA_BYTES)
    os.utime(audio, (1_700_000_000, 1_700_000_000))
    video_id = "zzzzzzzzzzz"
    video = youtube_cache_path(root, video_id, "video")
    video.parent.mkdir(parents=True)
    video.write_bytes(MEDIA_BYTES)
    os.utime(video, (1_600_000_000, 1_600_000_000))
    (video.parent / ".title.json").write_text(
        json.dumps({"title": "Evening", "uploader": "Studio"}), encoding="utf-8",
    )

    restarted_client = create_app(root).test_client()
    response = restarted_client.get(RECENT_ENDPOINT)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json() == {"items": [
        {"filename": "YouTube Video · Evening · Studio.mp4", "download_url": f"/api/cache/youtube/media/{video_id}/video"},
        {"filename": f"YouTube Audio · {VIDEO_ID}.m4a", "download_url": f"/api/cache/youtube/media/{VIDEO_ID}/audio"},
    ]}

    lan_url = "http://192.168.1.30:8666"
    with restarted_client.session_transaction(base_url=lan_url) as session:
        session[AGENT_ACCESS_SESSION_KEY] = True
    response = restarted_client.get(
        RECENT_ENDPOINT, base_url=lan_url, environ_overrides={"REMOTE_ADDR": "192.168.1.31"},
    )
    assert response.status_code == 403
    worker.assert_not_called()


def test_recent_youtube_media_list_caps_stable_results_and_ignores_unpublished_files(
    youtube_client: FlaskClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local-store"
    monkeypatch.setattr("app.core.youtube_cache._cache_creation_time_ns", lambda info: 1)
    for index in range(21):
        audio = youtube_cache_path(root, f"{index:011d}", "audio")
        audio.parent.mkdir(parents=True)
        audio.write_bytes(MEDIA_BYTES)

    empty = youtube_cache_path(root, "xxxxxxxxxxx", "audio")
    empty.parent.mkdir()
    empty.touch()
    stage = empty.parent / ".download-unfinished"
    stage.mkdir()
    (stage / empty.name).write_bytes(MEDIA_BYTES)
    if os.name != "nt":
        linked_directory = youtube_cache_path(root, "zzzzzzzzzzz", "audio")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / linked_directory.name).write_bytes(MEDIA_BYTES)
        linked_directory.parent.symlink_to(outside, target_is_directory=True)

        linked_file = youtube_cache_path(root, "yyyyyyyyyyy", "audio")
        linked_file.parent.mkdir()
        linked_file.symlink_to(outside / linked_directory.name)

    items = youtube_client.get(RECENT_ENDPOINT).get_json()["items"]

    assert [item["download_url"] for item in items] == [
        f"/api/cache/youtube/media/{index:011d}/audio" for index in range(20, 0, -1)
    ]


def test_youtube_job_coalesces_duplicate_requests_and_serves_the_completed_file(
    youtube_client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = Event()

    def finish_download(command: list[str], _url: str, **_kwargs) -> subprocess.CompletedProcess:
        assert released.wait(5)
        output = command[command.index("--output") + 1]
        Path(output.replace("%(ext)s", "m4a")).write_bytes(MEDIA_BYTES)
        metadata = Path(command[command.index("--print-to-file") + 2])
        metadata.write_text(
            json.dumps({"title": "现场 / Live", "uploader": "录音室"}), encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    worker = Mock(side_effect=finish_download)
    monkeypatch.setattr("app.core.youtube_cache.shutil.which", lambda name: f"/test-tools/{name}")
    monkeypatch.setattr("app.core.youtube_cache._downloader_runtime", lambda: (["yt-dlp"], [], None))
    monkeypatch.setattr("app.core.youtube_cache._verify_download", Mock())
    monkeypatch.setattr("app.core.youtube_cache.run_yt_dlp_with_retries", worker)
    try:
        started = youtube_client.post(DOWNLOAD_ENDPOINT, json={"url": VIDEO_URL, "mode": "audio"})
        assert started.status_code == 202
        status_url = started.get_json()["status_url"]
        assert youtube_client.get(status_url).get_json()["status"] == "running"

        destination_probe = Mock(side_effect=PermissionError("Synthetic transient destination sharing violation"))
        with monkeypatch.context() as in_progress:
            in_progress.setattr(
                "app.web.youtube_cache_routes.youtube_cache_path",
                destination_probe,
            )
            duplicate = youtube_client.post(
                DOWNLOAD_ENDPOINT,
                json={"url": f"https://youtu.be/{VIDEO_ID}?si=share", "mode": "audio"},
            )
        assert duplicate.status_code == 202, duplicate.get_json()
        assert duplicate.get_json()["status_url"] == status_url
        destination_probe.assert_not_called()
        other_mode = youtube_client.post(DOWNLOAD_ENDPOINT, json={"url": VIDEO_URL, "mode": "video"})
        assert other_mode.status_code == 409
    finally:
        released.set()

    result = wait_for_youtube_job(youtube_client, status_url)
    assert result["status"] == "completed", result
    assert result["filename"] == "YouTube Audio · 现场 Live · 录音室.m4a"
    assert result["size_bytes"] == len(MEDIA_BYTES)
    media = youtube_client.get(result["download_url"])
    assert media.status_code == 200
    assert media.data == MEDIA_BYTES
    disposition, parameters = parse_options_header(media.headers["Content-Disposition"])
    assert disposition == "attachment"
    assert parameters["filename"] == result["filename"]

    repeat = youtube_client.post(DOWNLOAD_ENDPOINT, json={"url": VIDEO_URL, "mode": "audio"})
    assert repeat.status_code == 202
    same_file = youtube_client.get(repeat.get_json()["status_url"]).get_json()
    assert same_file["status"] == "completed"
    assert same_file["cached"]
    assert same_file["download_url"] == result["download_url"]
    worker.assert_called_once()


def test_youtube_status_reports_only_current_measured_download_progress(
    youtube_client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    download_marker = "__CACHELIKES_YOUTUBE_PROGRESS__:download:"
    processing_marker = "__CACHELIKES_YOUTUBE_PROGRESS__:postprocess:"
    samples = (
        (
            f'1: {download_marker}{{"status":"downloading","downloaded_bytes":8192,"total_bytes":16384}}'
            '\t{"vcodec":"avc1","acodec":"none"}',
            {"phase": "downloading", "downloaded_bytes": 8192, "total_bytes": 16384, "stream": "video"},
        ),
        (
            f'{download_marker}{{"status":"downloading","downloaded_bytes":512,"total_bytes_estimate":2048}}'
            '\t{"vcodec":"none","acodec":"aac"}',
            {"phase": "downloading", "downloaded_bytes": 512, "total_bytes": None, "stream": "audio"},
        ),
        (
            f'{processing_marker}{{"status":"started","postprocessor":"SponsorBlock"}}',
            {"phase": "downloading", "downloaded_bytes": 512, "total_bytes": None, "stream": "audio"},
        ),
        (None, None),
        (
            f'{download_marker}{{"status":"downloading","downloaded_bytes":256,"total_bytes":1024}}'
            '\t{"vcodec":"avc1","acodec":"aac"}',
            {"phase": "downloading", "downloaded_bytes": 256, "total_bytes": 1024, "stream": "media"},
        ),
        (
            f'{processing_marker}{{"status":"started","postprocessor":"Merger"}}',
            {"phase": "processing", "downloaded_bytes": 256, "total_bytes": None},
        ),
    )
    releases = [Event() for _ in samples]
    reports = [Event() for _ in samples]
    finish = Event()

    def finish_download(command, _url, *, consume_stdout, on_retry, **_kwargs):
        assert "--newline" in command and "--progress" in command
        templates = [command[index + 1] for index, argument in enumerate(command) if argument == "--progress-template"]
        assert len(templates) == 2
        assert all("total_bytes_estimate" not in template for template in templates)
        for (line, _expected), release, report in zip(samples, releases, reports, strict=True):
            assert release.wait(5)
            if line is None:
                on_retry()
            else:
                assert consume_stdout(line)
            report.set()
        assert finish.wait(5)
        output = command[command.index("--output") + 1]
        Path(output.replace("%(ext)s", "mp4")).write_bytes(MEDIA_BYTES)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("app.core.youtube_cache.shutil.which", lambda name: f"/test-tools/{name}")
    monkeypatch.setattr("app.core.youtube_cache._downloader_runtime", lambda: (["yt-dlp"], [], None))
    monkeypatch.setattr("app.core.youtube_cache._verify_download", Mock())
    monkeypatch.setattr("app.core.youtube_cache.run_yt_dlp_with_retries", finish_download)
    status_path = youtube_client.post(DOWNLOAD_ENDPOINT, json={"url": VIDEO_URL, "mode": "video"}).get_json()["status_url"]
    try:
        assert youtube_client.get(status_path).get_json() == {"status": "running"}
        for (_line, expected), release, report in zip(samples, releases, reports, strict=True):
            release.set()
            assert report.wait(5)
            payload = {"status": "running", "progress": expected} if expected else {"status": "running"}
            assert youtube_client.get(status_path).get_json() == payload
    finally:
        for release in releases:
            release.set()
        finish.set()
    result = wait_for_youtube_job(youtube_client, status_path)
    assert result["status"] == "completed"
    assert "progress" not in result


def wait_for_youtube_job(client: FlaskClient, status_url: str) -> dict:
    deadline = monotonic() + 5
    delay = Event()
    while monotonic() < deadline:
        response = client.get(status_url)
        assert response.status_code == 200
        result = response.get_json()
        if result["status"] != "running":
            return result
        delay.wait(0.02)
    pytest.fail("The YouTube cache job did not finish.")


@pytest.mark.parametrize(
    ("payload", "filename"),
    (
        ({"title": "CON. live", "channel": "公开频道"}, "YouTube Audio · _CON. live · 公开频道.m4a"),
        ({"title": "CON. live"}, "YouTube Audio · _CON. live.m4a"),
        (None, "YouTube Audio · _CON. live.m4a"),
    ),
)
def test_existing_media_uploader_lookup_reuses_the_audio_and_legacy_title(
    youtube_client: FlaskClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, str] | None,
    filename: str,
) -> None:
    path = youtube_cache_path(tmp_path / "local-store", VIDEO_ID, "audio")
    path.parent.mkdir(parents=True)
    path.write_bytes(MEDIA_BYTES)
    before = path.stat()
    (path.parent / ".title.json").write_text(json.dumps("CON. live"), encoding="utf-8")

    def lookup(command: list[str], url: str, **kwargs) -> subprocess.CompletedProcess:
        assert command[-1] == url == VIDEO_URL
        assert "--skip-download" in command and "--no-playlist" in command
        assert "--output" not in command and "--extract-audio" not in command
        assert not kwargs["should_stop"]()
        stdout = json.dumps(payload) if payload is not None else ""
        return subprocess.CompletedProcess(command, int(payload is None), stdout=stdout, stderr="")

    worker = Mock(side_effect=lookup)
    monkeypatch.setattr("app.core.youtube_cache._downloader_runtime", lambda: (["yt-dlp"], [], None))
    monkeypatch.setattr("app.core.youtube_cache.run_yt_dlp_with_retries", worker)
    response = youtube_client.post(DOWNLOAD_ENDPOINT, json={"url": VIDEO_URL, "mode": "audio"})
    assert response.status_code == 202
    result = wait_for_youtube_job(youtube_client, response.get_json()["status_url"])
    assert result["status"] == "completed", result
    assert result["cached"] and result["filename"] == filename
    assert path.read_bytes() == MEDIA_BYTES
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    media = youtube_client.get(result["download_url"])
    assert parse_options_header(media.headers["Content-Disposition"])[1]["filename"] == filename
    if payload is not None:
        repeat = youtube_client.post(DOWNLOAD_ENDPOINT, json={"url": VIDEO_URL, "mode": "audio"})
        assert youtube_client.get(repeat.get_json()["status_url"]).get_json()["filename"] == filename
    worker.assert_called_once()


def test_uploader_filename_preserves_extension_and_fits_a_utf8_basename(tmp_path: Path) -> None:
    path = youtube_cache_path(tmp_path, VIDEO_ID, "video")
    path.parent.mkdir(parents=True)
    path.write_bytes(MEDIA_BYTES)
    (path.parent / ".title.json").write_text(
        json.dumps({"title": "音" * 100, "uploader": "樂" * 30}), encoding="utf-8",
    )

    result = read_cached_youtube_media(path)

    assert result is not None
    assert len(result.filename.encode("utf-8")) <= 255
    assert result.filename.startswith("YouTube Video · 音")
    assert result.filename.endswith(f" · {'樂' * 26}.mp4")


def test_runtime_shutdown_stops_the_youtube_job_and_releases_its_cache_lock(
    youtube_client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = Event()

    def wait_for_shutdown(*_args, should_stop: Callable[[], bool], **_kwargs) -> YouTubeCacheResult:
        worker_started.set()
        deadline = monotonic() + 5
        delay = Event()
        while not should_stop() and monotonic() < deadline:
            delay.wait(0.01)
        assert should_stop()
        raise YouTubeCacheError("The YouTube download stopped.")

    monkeypatch.setattr("app.web.youtube_cache_routes.download_youtube_media", wait_for_shutdown)
    extensions = youtube_client.application.extensions
    coordinator = extensions["cache_task_coordinator"]
    try:
        started = youtube_client.post(DOWNLOAD_ENDPOINT, json={"url": VIDEO_URL, "mode": "video"})
        assert started.status_code == 202
        assert worker_started.wait(5)
        assert "youtube:media" in coordinator.task_states()
    finally:
        extensions["runtime_shutdown"]()

    status = youtube_client.get(started.get_json()["status_url"]).get_json()
    assert status == {"status": "failed", "error": "The YouTube download stopped."}
    assert "youtube:media" not in coordinator.task_states()
    rejected = youtube_client.post(DOWNLOAD_ENDPOINT, json={"url": VIDEO_URL, "mode": "video"})
    assert rejected.status_code == 503
