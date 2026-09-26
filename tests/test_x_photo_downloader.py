"""Exercise original X photo transfers and complete mixed-media retries offline.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from contextlib import contextmanager
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import subprocess
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image
import pytest

from app.core.cache_catalog import LocalTweetCacheIndex
from app.core.config import CrawlConfig
from app.core.downloader import (
    MEDIA_MARKER_PREFIX,
    METADATA_MARKER_PREFIX,
    YtDlpInterrupted,
    download_tweet_media,
)
from app.core.state import TaskState
from app.core.x_photo_downloader import (
    XMediaPost,
    XPhotoDownloadInterrupted,
    download_x_photos,
    normalize_x_photo_url,
)


TWEET_URL = "https://x.com/photo_fixture/status/123456789"
PHOTO_URL = "https://pbs.twimg.com/media/fixture_one?format=png&name=small"
SECOND_PHOTO_URL = "https://pbs.twimg.com/media/fixture_two?format=png&name=medium"


def _image_bytes(color: str = "blue") -> bytes:
    stream = BytesIO()
    Image.new("RGB", (13, 9), color).save(stream, format="PNG")
    return stream.getvalue()


def _video_output(path: Path) -> str:
    metadata = {
        "id": path.stem,
        "display_id": "123456789",
        "uploader_id": "photo_fixture",
        "webpage_url": TWEET_URL,
        "extractor_key": "Twitter",
        "_type": "video",
    }
    return f"{MEDIA_MARKER_PREFIX}{path}\n{METADATA_MARKER_PREFIX}{json.dumps(metadata)}\n"


class _PhotoResponse:
    """Expose real image bytes through an HTTP-like bounded stream."""

    def __init__(
        self,
        payload: bytes,
        url: str,
        *,
        declared_length: int | None = None,
        content_type: str = "image/png",
        read_error: bool = False,
    ) -> None:
        self._stream = BytesIO(payload)
        self._url = url
        self._read_error = read_error
        self.read_calls = 0
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if declared_length is not None:
            self.headers["Content-Length"] = str(declared_length)
        self.status = 200

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if self._read_error and self.read_calls > 1:
            raise OSError("Simulated interrupted stream.")
        return self._stream.read(min(size, 12) if self._read_error else size)


@contextmanager
def _response_context(response: _PhotoResponse):
    yield response


def _transfer(plan: XMediaPost, output_dir: Path, max_bytes: int = 1_048_576):
    return download_x_photos(plan, output_dir, max_bytes, lambda: False, time.monotonic() + 30)


@pytest.mark.parametrize(
    "source_url",
    (
        PHOTO_URL,
        "https://pbs.twimg.com/media/fixture_one.png?name=large",
    ),
)
def test_photo_url_requests_the_original_resolution(source_url: str) -> None:
    normalized = normalize_x_photo_url(source_url)
    parsed = urlsplit(normalized)
    assert parsed.scheme == "https"
    assert parsed.netloc == "pbs.twimg.com"
    assert parsed.path.startswith("/media/")
    assert parse_qs(parsed.query)["name"] == ["orig"]


@pytest.mark.parametrize(
    "source_url",
    (
        "http://pbs.twimg.com/media/image.png",
        "https://pbs.twimg.com.evil.example/media/image.png",
        "https://user:password@pbs.twimg.com/media/image.png",
        "https://pbs.twimg.com:8443/media/image.png",
        "https://pbs.twimg.com/profile_images/image.png",
        "https://pbs.twimg.com/media/../secret.png",
        "https://pbs.twimg.com/media/%2e%2e/secret.png",
        "https://example.com/media/image.png",
        "file:///tmp/image.png",
    ),
)
def test_photo_url_rejects_untrusted_sources(source_url: str) -> None:
    assert normalize_x_photo_url(source_url) == ""


def test_photo_transfer_reuses_valid_originals_without_network(tmp_path: Path) -> None:
    payload = _image_bytes()
    plan = XMediaPost(TWEET_URL, (PHOTO_URL,))
    response = _PhotoResponse(payload, normalize_x_photo_url(PHOTO_URL))
    with patch(
        "app.core.x_photo_downloader._open_photo_url",
        return_value=_response_context(response),
    ) as request:
        first = _transfer(plan, tmp_path / "x")
        second = _transfer(plan, tmp_path / "x")

    assert request.call_count == 1
    assert first.downloaded_count == 1
    assert second.downloaded_count == 0
    assert first.paths == second.paths
    assert len(first.paths) == 1
    assert first.paths[0].read_bytes() == payload
    assert first.paths[0].resolve().is_relative_to((tmp_path / "x").resolve())
    with Image.open(first.paths[0]) as image:
        assert image.size == (13, 9)
        image.verify()


def test_lowered_size_limit_preserves_a_cached_original_without_network(tmp_path: Path) -> None:
    payload = _image_bytes()
    plan = XMediaPost(TWEET_URL, (PHOTO_URL,))
    with patch(
        "app.core.x_photo_downloader._open_photo_url",
        return_value=_response_context(_PhotoResponse(payload, normalize_x_photo_url(PHOTO_URL))),
    ) as request:
        first = _transfer(plan, tmp_path / "x")
        limited = _transfer(plan, tmp_path / "x", len(payload) - 1)
    assert request.call_count == 1
    assert limited.downloaded_count == 0
    assert limited.skipped_size == 1
    assert first.paths[0].read_bytes() == payload


def test_photo_stop_preserves_the_completed_original(tmp_path: Path) -> None:
    payload = _image_bytes()
    output_dir = tmp_path / "x"
    plan = XMediaPost(TWEET_URL, (PHOTO_URL, SECOND_PHOTO_URL))
    with patch(
        "app.core.x_photo_downloader._open_photo_url",
        return_value=_response_context(_PhotoResponse(payload, normalize_x_photo_url(PHOTO_URL))),
    ) as request:
        with pytest.raises(XPhotoDownloadInterrupted) as stopped:
            download_x_photos(
                plan, output_dir, 1_048_576,
                lambda: bool(list(output_dir.rglob("*.png"))), time.monotonic() + 30,
            )
    assert stopped.value.reason == "stopped"
    assert stopped.value.result.downloaded_count == 1
    assert len(stopped.value.result.paths) == 1
    assert stopped.value.result.paths[0].read_bytes() == payload
    assert request.call_count == 1


def test_photo_deadline_expires_before_network(tmp_path: Path) -> None:
    with patch("app.core.x_photo_downloader._open_photo_url") as request:
        with pytest.raises(XPhotoDownloadInterrupted) as expired:
            download_x_photos(
                XMediaPost(TWEET_URL, (PHOTO_URL,)), tmp_path / "x", 1_048_576,
                lambda: False, time.monotonic() - 1,
            )
    assert expired.value.reason == "timed_out"
    assert expired.value.result.downloaded_count == 0
    request.assert_not_called()


def test_photo_transfer_rejects_entire_unsafe_plan_before_network(tmp_path: Path) -> None:
    plan = XMediaPost(TWEET_URL, (PHOTO_URL, "https://example.com/private.png"))
    with patch("app.core.x_photo_downloader._open_photo_url") as request:
        with pytest.raises(RuntimeError):
            _transfer(plan, tmp_path / "x")
    request.assert_not_called()
    assert not list(tmp_path.rglob("*.png"))


def test_photo_transfer_rejects_cross_origin_final_response(tmp_path: Path) -> None:
    response = _PhotoResponse(_image_bytes(), "https://example.com/private.png")
    with patch(
        "app.core.x_photo_downloader._open_photo_url",
        return_value=_response_context(response),
    ):
        with pytest.raises(RuntimeError):
            _transfer(XMediaPost(TWEET_URL, (PHOTO_URL,)), tmp_path / "x")
    assert response.read_calls == 0
    assert not list(tmp_path.rglob("*.png"))


def test_photo_transfer_rejects_symlinked_output_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    output_dir = tmp_path / "x"
    output_dir.symlink_to(outside, target_is_directory=True)
    with patch("app.core.x_photo_downloader._open_photo_url") as request:
        with pytest.raises(RuntimeError):
            _transfer(XMediaPost(TWEET_URL, (PHOTO_URL,)), output_dir)
    request.assert_not_called()
    assert not list(outside.iterdir())


@pytest.mark.parametrize("advertise_size", (False, True))
def test_photo_transfer_stops_oversize_before_final_write(tmp_path: Path, advertise_size: bool) -> None:
    payload = _image_bytes()
    response = _PhotoResponse(
        payload,
        normalize_x_photo_url(PHOTO_URL),
        declared_length=len(payload) if advertise_size else None,
    )
    with patch(
        "app.core.x_photo_downloader._open_photo_url",
        return_value=_response_context(response),
    ):
        result = _transfer(XMediaPost(TWEET_URL, (PHOTO_URL,)), tmp_path / "x", len(payload) - 1)
    assert result.skipped_size == 1
    assert result.downloaded_count == 0
    assert result.paths == ()
    assert not list(tmp_path.rglob("*.png"))
    if advertise_size:
        assert response.read_calls == 0


@pytest.mark.parametrize("payload", (b"<html>not an image</html>", _image_bytes()[:24]))
def test_photo_transfer_rejects_undecodable_payloads(tmp_path: Path, payload: bytes) -> None:
    response = _PhotoResponse(payload, normalize_x_photo_url(PHOTO_URL))
    with patch(
        "app.core.x_photo_downloader._open_photo_url",
        return_value=_response_context(response),
    ):
        with pytest.raises(RuntimeError):
            _transfer(XMediaPost(TWEET_URL, (PHOTO_URL,)), tmp_path / "x")
    assert not list(tmp_path.rglob("*.png"))


def test_interrupted_photo_retry_preserves_completed_photos(tmp_path: Path) -> None:
    first_payload = _image_bytes()
    second_payload = _image_bytes("green")
    plan = XMediaPost(TWEET_URL, (PHOTO_URL, SECOND_PHOTO_URL))
    requests: list[str] = []

    @contextmanager
    def open_photo(url: str, timeout: float):
        requests.append(url)
        second = "fixture_two" in url
        yield _PhotoResponse(
            second_payload if second else first_payload,
            url,
            read_error=second and requests.count(url) == 1,
        )

    with patch("app.core.x_photo_downloader._open_photo_url", side_effect=open_photo):
        with pytest.raises((RuntimeError, OSError)):
            _transfer(plan, tmp_path / "x")
        completed = [path for path in (tmp_path / "x").rglob("*.png")]
        assert len(completed) == 1
        assert completed[0].read_bytes() == first_payload
        result = _transfer(plan, tmp_path / "x")

    assert result.downloaded_count == 1
    assert len(result.paths) == 2
    assert sorted(path.read_bytes() for path in result.paths) == sorted((first_payload, second_payload))
    assert requests.count(normalize_x_photo_url(PHOTO_URL)) == 1
    assert requests.count(normalize_x_photo_url(SECOND_PHOTO_URL)) == 2


def test_photo_item_budget_reuses_existing_files_before_fetching_one_more(tmp_path: Path) -> None:
    plan = XMediaPost(TWEET_URL, (PHOTO_URL, SECOND_PHOTO_URL))

    @contextmanager
    def open_photo(url: str, timeout: float):
        yield _PhotoResponse(_image_bytes("green" if "fixture_two" in url else "blue"), url)

    with patch("app.core.x_photo_downloader._open_photo_url", side_effect=open_photo) as request:
        first = download_x_photos(
            plan, tmp_path / "x", 1_048_576, lambda: False,
            time.monotonic() + 30, max_new_items=1,
        )
        assert first.downloaded_count == 1
        assert len(first.paths) == 1
        second = download_x_photos(
            plan, tmp_path / "x", 1_048_576, lambda: False,
            time.monotonic() + 30, max_new_items=1,
        )
    assert second.downloaded_count == 1
    assert len(second.paths) == 2
    assert request.call_count == 2


@pytest.mark.parametrize("has_video", (False, True))
def test_capped_post_resumes_the_unfinished_plan_after_catalog_reload(tmp_path: Path, has_video: bool) -> None:
    output_dir = tmp_path / "x"
    plan = XMediaPost(
        TWEET_URL, (PHOTO_URL,) if has_video else (PHOTO_URL, SECOND_PHOTO_URL),
        has_video=has_video,
    )
    cache_index = LocalTweetCacheIndex.build(output_dir)
    video_path = output_dir / "photo_fixture" / "123456789" / "123456789.mp4"

    @contextmanager
    def open_photo(url: str, timeout: float):
        yield _PhotoResponse(_image_bytes("green" if "fixture_two" in url else "blue"), url)

    def run_video(*_args, **_kwargs):
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"completed video")
        return subprocess.CompletedProcess([], 0, stdout=f"{MEDIA_MARKER_PREFIX}{video_path}\n", stderr="")

    config = CrawlConfig(x_browser="safari")
    with patch("app.core.x_photo_downloader._open_photo_url", side_effect=open_photo) as request, patch(
        "app.core.downloader.ensure_yt_dlp_available", return_value=["yt-dlp"]
    ), patch("app.core.downloader.run_yt_dlp_with_retries", side_effect=run_video) as video:
        first = download_tweet_media(
            TWEET_URL, output_dir, config, TaskState("test"), remaining_media_items=1,
            cache_index=cache_index, media_post=plan,
        )
        assert first.downloaded_media_count == 1
        assert not cache_index.contains_complete_cache(TWEET_URL)
        video.assert_not_called()
        restored_index = LocalTweetCacheIndex.build(output_dir)
        assert not restored_index.contains_complete_cache(TWEET_URL)
        second = download_tweet_media(
            TWEET_URL, output_dir, config, TaskState("test"), remaining_media_items=1,
            cache_index=restored_index, media_post=plan,
        )
    assert second.downloaded_media_count == 1
    assert request.call_count == (1 if has_video else 2)
    assert video.call_count == int(has_video)
    assert restored_index.contains_complete_cache(TWEET_URL)
    assert restored_index.summarize() == ((1, 1, 1) if has_video else (1, 2, 0))


def test_mixed_post_retries_video_after_preserving_downloaded_photo(tmp_path: Path) -> None:
    output_dir = tmp_path / "x"
    cache_index = LocalTweetCacheIndex.build(output_dir)
    plan = XMediaPost(TWEET_URL, (PHOTO_URL,), has_video=True)
    payload = _image_bytes()
    video_path = output_dir / "photo_fixture" / "123456789" / "123456789.mp4"
    calls = 0

    def run_video(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess([], 1, stdout="", stderr="Requested video format unavailable.")
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"completed video")
        return subprocess.CompletedProcess([], 0, stdout=f"{MEDIA_MARKER_PREFIX}{video_path}\n", stderr="")

    @contextmanager
    def open_photo(url: str, timeout: float):
        yield _PhotoResponse(payload, url)

    config = CrawlConfig(x_browser="safari")
    with patch("app.core.x_photo_downloader._open_photo_url", side_effect=open_photo) as photo, patch(
        "app.core.downloader.ensure_yt_dlp_available", return_value=["yt-dlp"]
    ), patch("app.core.downloader.run_yt_dlp_with_retries", side_effect=run_video):
        with pytest.raises(RuntimeError):
            download_tweet_media(TWEET_URL, output_dir, config, TaskState("test"), cache_index=cache_index, media_post=plan)
        photos = list(output_dir.rglob("*.png"))
        assert len(photos) == 1
        assert photos[0].read_bytes() == payload
        retried = download_tweet_media(
            TWEET_URL, output_dir, config, TaskState("test"), cache_index=cache_index, media_post=plan
        )
        repeated = download_tweet_media(
            TWEET_URL, output_dir, config, TaskState("test"), cache_index=cache_index, media_post=plan
        )
    assert calls == 2
    assert photo.call_count == 1
    assert retried.downloaded_video_count == 1
    assert repeated.skipped
    assert photos[0].read_bytes() == payload
    assert video_path.read_bytes() == b"completed video"
    assert cache_index.summarize() == (1, 1, 1)


def test_changed_photo_plan_fetches_only_the_added_original(tmp_path: Path) -> None:
    output_dir = tmp_path / "x"
    cache_index = LocalTweetCacheIndex.build(output_dir)
    config = CrawlConfig(x_browser="safari")

    @contextmanager
    def open_photo(url: str, timeout: float):
        yield _PhotoResponse(_image_bytes("green" if "fixture_two" in url else "blue"), url)

    with patch("app.core.x_photo_downloader._open_photo_url", side_effect=open_photo) as request, patch(
        "app.core.downloader.ensure_yt_dlp_available", side_effect=AssertionError("Photo-only plan launched video worker.")
    ):
        first = download_tweet_media(
            TWEET_URL, output_dir, config, TaskState("test"), cache_index=cache_index,
            media_post=XMediaPost(TWEET_URL, (PHOTO_URL,)),
        )
        second = download_tweet_media(
            TWEET_URL, output_dir, config, TaskState("test"), cache_index=cache_index,
            media_post=XMediaPost(TWEET_URL, (PHOTO_URL, SECOND_PHOTO_URL)),
        )
    assert first.downloaded_image_count == 1
    assert second.downloaded_image_count == 1
    assert request.call_count == 2
    assert cache_index.summarize()[1] == 2


def test_existing_video_does_not_hide_or_overwrite_a_new_photo(tmp_path: Path) -> None:
    output_dir = tmp_path / "x"
    video_path = output_dir / "photo_fixture" / "123456789" / "123456789.mp4"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"previously completed video")
    cache_index = LocalTweetCacheIndex.build(output_dir)
    cache_index.register(TWEET_URL, video_path.parent, {"_type": "video", "display_id": "123456789"})
    payload = _image_bytes()
    with patch(
        "app.core.x_photo_downloader._open_photo_url",
        return_value=_response_context(_PhotoResponse(payload, normalize_x_photo_url(PHOTO_URL))),
    ), patch("app.core.downloader.ensure_yt_dlp_available", side_effect=AssertionError("Existing video was re-requested.")):
        result = download_tweet_media(
            TWEET_URL, output_dir, CrawlConfig(x_browser="safari"), TaskState("test"),
            cache_index=cache_index, media_post=XMediaPost(TWEET_URL, (PHOTO_URL,), has_video=True),
        )
    assert result.downloaded_image_count == 1
    assert result.downloaded_video_count == 0
    assert video_path.read_bytes() == b"previously completed video"
    assert [path.read_bytes() for path in output_dir.rglob("*.png")] == [payload]
    assert cache_index.summarize() == (1, 1, 1)


def test_observed_video_only_plan_extends_an_existing_photo_cache(tmp_path: Path) -> None:
    output_dir = tmp_path / "x"
    cache_index = LocalTweetCacheIndex.build(output_dir)
    config = CrawlConfig(x_browser="safari")
    payload = _image_bytes()
    video_path = output_dir / "photo_fixture" / "987654321" / "987654321.mp4"

    def run_video(*_args, **_kwargs):
        video_path.parent.mkdir(parents=True)
        video_path.write_bytes(b"newly observed video")
        return subprocess.CompletedProcess([], 0, stdout=_video_output(video_path), stderr="")

    with patch(
        "app.core.x_photo_downloader._open_photo_url",
        return_value=_response_context(_PhotoResponse(payload, normalize_x_photo_url(PHOTO_URL))),
    ) as photo, patch(
        "app.core.downloader.ensure_yt_dlp_available", return_value=["yt-dlp"]
    ), patch("app.core.downloader.run_yt_dlp_with_retries", side_effect=run_video) as video:
        first = download_tweet_media(
            TWEET_URL, output_dir, config, TaskState("test"), cache_index=cache_index,
            media_post=XMediaPost(TWEET_URL, (PHOTO_URL,)),
        )
        assert first.downloaded_image_count == 1
        assert cache_index.contains_complete_cache(TWEET_URL)
        video.assert_not_called()
        second = download_tweet_media(
            TWEET_URL, output_dir, config, TaskState("test"), cache_index=cache_index,
            media_post=XMediaPost(TWEET_URL, (), has_video=True),
        )

    assert photo.call_count == 1
    assert video.call_count == 1
    assert second.downloaded_video_count == 1
    assert second.downloaded_media_count == 1
    assert not second.skipped
    assert [path.read_bytes() for path in output_dir.rglob("*.png")] == [payload]
    assert video_path.read_bytes() == b"newly observed video"
    assert cache_index.contains_complete_cache(TWEET_URL)
    assert cache_index.summarize() == (1, 1, 1)


@pytest.mark.parametrize("has_photo", (False, True))
def test_video_plan_completes_with_media_id_directory_distinct_from_status(
    tmp_path: Path, has_photo: bool,
) -> None:
    output_dir = tmp_path / "x"
    cache_index = LocalTweetCacheIndex.build(output_dir)
    video_path = output_dir / "photo_fixture" / "987654321" / "987654321.mp4"
    plan = XMediaPost(TWEET_URL, (PHOTO_URL,) if has_photo else (), has_video=True)

    def run_video(*_args, **_kwargs):
        video_path.parent.mkdir(parents=True)
        video_path.write_bytes(b"completed actual media ID video")
        return subprocess.CompletedProcess([], 0, stdout=_video_output(video_path), stderr="")

    with patch(
        "app.core.x_photo_downloader._open_photo_url",
        return_value=_response_context(_PhotoResponse(_image_bytes(), normalize_x_photo_url(PHOTO_URL))),
    ) as photo, patch(
        "app.core.downloader.ensure_yt_dlp_available", return_value=["yt-dlp"]
    ), patch("app.core.downloader.run_yt_dlp_with_retries", side_effect=run_video) as video:
        result = download_tweet_media(
            TWEET_URL, output_dir, CrawlConfig(x_browser="safari"), TaskState("test"),
            cache_index=cache_index, media_post=plan,
        )

    assert result.downloaded_video_count == 1
    assert result.downloaded_media_count == 1 + int(has_photo)
    assert photo.call_count == int(has_photo)
    assert video.call_count == 1
    assert video_path.parent in cache_index.lookup_directories(TWEET_URL)
    restored = LocalTweetCacheIndex.build(output_dir)
    assert video_path.parent in restored.lookup_directories(TWEET_URL)
    assert restored.contains_complete_cache(TWEET_URL)
    assert restored.summarize() == (1, int(has_photo), 1)


@pytest.mark.parametrize("failure_mode", ("stopped", "nonzero", "transient", "budget"))
def test_partial_actual_media_id_video_remains_pending_and_resumes_after_catalog_reload(
    tmp_path: Path, failure_mode: str,
) -> None:
    output_dir = tmp_path / "x"
    cache_index = LocalTweetCacheIndex.build(output_dir)
    config = CrawlConfig(x_browser="safari")
    has_photo = failure_mode != "budget"
    plan = XMediaPost(TWEET_URL, (PHOTO_URL,) if has_photo else (), has_video=True)
    first_video = output_dir / "photo_fixture" / "987654321" / "987654321.mp4"
    second_video = output_dir / "photo_fixture" / "987654322" / "987654322.mp4"
    marker = output_dir / "photos" / "123456789" / ".x-media-plan.json"
    preserved_bytes = b"completed first video before interrupted second video"
    payload = _image_bytes()
    calls = 0
    archives: list[Path] = []

    def run_video(command, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        assert "--no-overwrites" in command
        archive = Path(command[command.index("--download-archive") + 1])
        archives.append(archive)
        archived = set(archive.read_text(encoding="utf-8").splitlines())
        if calls == 1:
            assert "twitter 987654321" not in archived
            first_video.parent.mkdir(parents=True)
            first_video.write_bytes(preserved_bytes)
            completed_output = _video_output(first_video)
            if failure_mode == "stopped":
                raise YtDlpInterrupted("stopped", completed_output)
            if failure_mode == "budget":
                assert command[command.index("--max-downloads") + 1] == "1"
                return subprocess.CompletedProcess(
                    [], 101, stdout=completed_output,
                    stderr="Maximum number of downloads reached, stopping due to --max-downloads.",
                )
            error = (
                "HTTP Error 503: Service Unavailable"
                if failure_mode == "transient"
                else "Requested video format unavailable."
            )
            return subprocess.CompletedProcess([], 1, stdout=completed_output, stderr=error)
        assert first_video.read_bytes() == preserved_bytes
        assert "twitter 987654321" in archived
        assert "twitter 987654322" not in archived
        if failure_mode == "budget":
            assert command[command.index("--max-downloads") + 1] == "1"
        second_video.parent.mkdir(parents=True)
        second_video.write_bytes(b"remaining video completed on retry")
        replayed_output = _video_output(first_video) if failure_mode != "budget" else ""
        return subprocess.CompletedProcess(
            [], 0, stdout=replayed_output + _video_output(second_video), stderr="",
        )

    with patch(
        "app.core.x_photo_downloader._open_photo_url",
        return_value=_response_context(_PhotoResponse(payload, normalize_x_photo_url(PHOTO_URL))),
    ) as photo, patch(
        "app.core.downloader.ensure_yt_dlp_available", return_value=["yt-dlp"]
    ), patch("app.core.downloader.run_yt_dlp_with_retries", side_effect=run_video) as video:
        if failure_mode in {"stopped", "budget"}:
            interrupted = download_tweet_media(
                TWEET_URL, output_dir, config, TaskState("test"),
                cache_index=cache_index, media_post=plan,
                remaining_media_items=1 if failure_mode == "budget" else None,
            )
            assert interrupted.stopped is (failure_mode == "stopped")
            assert interrupted.downloaded_video_count == 1
            assert interrupted.downloaded_media_count == 1 + int(has_photo)
        else:
            with pytest.raises(RuntimeError):
                download_tweet_media(
                    TWEET_URL, output_dir, config, TaskState("test"),
                    cache_index=cache_index, media_post=plan,
                )
        assert video.call_count == 1
        assert all(not archive.exists() for archive in archives)
        assert first_video.read_bytes() == preserved_bytes
        assert first_video.parent in cache_index.lookup_directories(TWEET_URL)
        assert not cache_index.contains_complete_cache(TWEET_URL)
        pending = json.loads(marker.read_text(encoding="utf-8"))
        assert pending["complete"] is False
        assert pending["video_complete"] is False
        restored = LocalTweetCacheIndex.build(output_dir)
        assert first_video.parent in restored.lookup_directories(TWEET_URL)
        assert not restored.contains_complete_cache(TWEET_URL)
        retried = download_tweet_media(
            TWEET_URL, output_dir, config, TaskState("test"),
            cache_index=restored, media_post=plan,
            remaining_media_items=1 if failure_mode == "budget" else None,
        )
        repeated = download_tweet_media(
            TWEET_URL, output_dir, config, TaskState("test"),
            cache_index=restored, media_post=plan,
        )

    assert video.call_count == 2
    assert photo.call_count == int(has_photo)
    assert retried.downloaded_video_count == 1
    assert repeated.skipped
    assert first_video.read_bytes() == preserved_bytes
    assert [path.read_bytes() for path in output_dir.rglob("*.png")] == ([payload] if has_photo else [])
    complete = json.loads(marker.read_text(encoding="utf-8"))
    assert complete["complete"] is True
    assert complete["video_complete"] is True
    assert restored.contains_complete_cache(TWEET_URL)
    assert restored.summarize() == (1, int(has_photo), 2)
    assert all(not archive.exists() for archive in archives)


@pytest.mark.parametrize("blocked_by", ("deletion", "inflight"))
def test_photo_plan_preserves_deletion_and_inflight_admission(tmp_path: Path, blocked_by: str) -> None:
    output_dir = tmp_path / "x"
    cache_index = LocalTweetCacheIndex.build(output_dir)
    if blocked_by == "inflight":
        assert cache_index.claim(TWEET_URL)
    with patch(
        "app.core.downloader.BrowserDeletionCatalog.is_excluded", return_value=blocked_by == "deletion"
    ), patch("app.core.x_photo_downloader._open_photo_url") as request:
        result = download_tweet_media(
            TWEET_URL, output_dir, CrawlConfig(x_browser="safari"), TaskState("test"),
            cache_index=cache_index, media_post=XMediaPost(TWEET_URL, (PHOTO_URL,)),
        )
    assert result.skipped
    request.assert_not_called()


def test_photo_plan_must_match_the_requested_post(tmp_path: Path) -> None:
    with patch("app.core.x_photo_downloader._open_photo_url") as request:
        with pytest.raises(RuntimeError):
            download_tweet_media(
                TWEET_URL, tmp_path / "x", CrawlConfig(x_browser="safari"), TaskState("test"),
                media_post=XMediaPost("https://x.com/photo_fixture/status/999", (PHOTO_URL,)),
            )
    request.assert_not_called()
