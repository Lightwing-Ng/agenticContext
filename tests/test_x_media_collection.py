"""Verify X photo metadata remains bound to primary liked posts."""

# Code version: v1.0.0-codex.0

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import CrawlConfig
from app.core.downloader import DownloadResult
from app.core.scraper import collect_liked_tweet_urls, collect_liked_tweet_urls_from_rendered_page
from app.core.service import CacheLikesService
from app.core.state import TaskSnapshot, TaskState
from app.core.x_photo_downloader import XMediaPost


def test_rendered_media_callback_deduplicates_and_rejects_uncollected_posts() -> None:
    page = MagicMock(url="https://x.com/liker/likes")
    links = json.dumps(["https://x.com/poster/status/123", "https://x.com/poster/status/456"])
    media = json.dumps([
        {"tweet_url": "https://x.com/poster/status/123", "photo_urls": [
            "https://pbs.twimg.com/media/photo?format=jpg&name=small",
            "https://pbs.twimg.com/media/photo?format=jpg&name=small",
            "https://foreign.example/media/photo.jpg",
        ], "has_video": False},
        {"tweet_url": "https://x.com/poster/status/456", "photo_urls": [], "has_video": True},
        {"tweet_url": "https://x.com/quoted/status/999", "photo_urls": ["https://pbs.twimg.com/media/quote.jpg"]},
    ])
    unloaded_media = json.dumps([
        {"tweet_url": "https://x.com/poster/status/123", "photo_urls": [], "has_video": False},
        {"tweet_url": "https://x.com/poster/status/456", "photo_urls": [], "has_video": False},
    ])
    page.evaluate.side_effect = [links, media, "scrolled", links, unloaded_media]
    batches = []

    urls = collect_liked_tweet_urls_from_rendered_page(
        page, "liker", page.url,
        CrawlConfig(max_scroll_rounds=2, stale_round_limit=1, scroll_pause_seconds=0.2),
        TaskState("test", snapshot_factory=TaskSnapshot), on_media_posts=batches.append,
    )

    assert urls == ["https://x.com/poster/status/123", "https://x.com/poster/status/456"]
    assert batches == [[
        XMediaPost(urls[0], ("https://pbs.twimg.com/media/photo?format=jpg&name=small",)),
        XMediaPost(urls[1], (), True),
    ]]


def test_safari_entry_forwards_media_callback() -> None:
    callback = MagicMock()
    with patch("app.core.scraper.detect_safari_x_account_handle", return_value="liker"), patch(
        "app.core.scraper.collect_liked_tweet_urls_via_safari", return_value=["https://x.com/poster/status/123"]
    ) as collect:
        collect_liked_tweet_urls(
            CrawlConfig(x_browser="safari"), TaskState("test", snapshot_factory=TaskSnapshot),
            on_media_posts=callback,
        )
    assert collect.call_args.kwargs["on_media_posts"] is callback


def test_safari_media_worker_forwards_matching_photo_plan(tmp_path: Path) -> None:
    state = TaskState("test", snapshot_factory=TaskSnapshot)
    service = CacheLikesService(state)
    photo = XMediaPost("https://x.com/poster/status/123", ("https://pbs.twimg.com/media/photo.jpg",))
    video = XMediaPost("https://x.com/poster/status/456", (), True)

    def collect(_config, _state, *, on_text_posts, on_media_posts, should_stop):
        assert not should_stop()
        assert on_text_posts is not None
        on_media_posts([photo, video])
        return "liker", ["https://twitter.com/poster/status/123", video.tweet_url]

    with patch("app.core.service.LOCAL_STORE_ROOT", tmp_path), patch(
        "app.core.service.collect_liked_tweet_urls", side_effect=collect
    ), patch("app.core.service.LocalTweetCacheIndex.build"), patch(
        "app.core.service.download_tweet_media", return_value=DownloadResult(downloaded_media_count=1)
    ) as download:
        service._run(CrawlConfig(x_browser="safari", download_workers=1, max_media_items=10))

    assert state.snapshot()["phase"] == "finished"
    assert [call.args[0] for call in download.call_args_list] == [photo.tweet_url, video.tweet_url]
    assert [call.kwargs["media_post"] for call in download.call_args_list] == [photo, video]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed to execute media ownership selectors")
def test_media_dom_selector_excludes_quoted_photos_video_and_foreign_images() -> None:
    page = MagicMock(url="https://x.com/liker/likes")
    page.evaluate.side_effect = ["[]", "[]"]
    collect_liked_tweet_urls_from_rendered_page(
        page, "liker", page.url, CrawlConfig(max_scroll_rounds=1),
        TaskState("test", snapshot_factory=TaskSnapshot), on_media_posts=lambda _posts: None,
    )
    media_source = page.evaluate.call_args_list[1].args[0]
    fixture = r"""
const fs = require('node:fs');
const source = JSON.parse(fs.readFileSync(0, 'utf8'));
global.location = new URL('https://x.com/liker/likes');
const timestamp = href => ({closest: () => ({href})});
const article = id => ({
    id, photos: [], videos: [],
    querySelector: selector => selector === 'time' ? timestamp(`https://x.com/poster/status/${id}`) : null,
    querySelectorAll(selector) {
        if (selector === '[data-testid="tweetPhoto"]') return this.photos;
        if (selector === '[data-testid="videoPlayer"], video') return this.videos;
        throw Error('Unexpected primary media selector');
    },
});
const first = article('123');
const second = article('456');
const media = (owner, status, urls = [], quote = false, quotedStatus = '') => ({
    closest(selector) {
        if (selector === 'article[data-testid="tweet"]') return owner;
        if (selector === '[data-testid="quoteTweet"]') return quote ? {} : null;
        if (selector === 'a[href*="/status/"]') return status ? {href: `https://x.com/poster/status/${status}/photo/1`} : null;
        if (selector === '[role="link"]') return quotedStatus ? {querySelector: () => timestamp(`https://x.com/quoted/status/${quotedStatus}`)} : null;
        throw Error('Unexpected media owner selector');
    },
    querySelectorAll: () => urls.map(src => ({src})),
});
first.photos = [
    media(first, '123', ['https://pbs.twimg.com/media/own?format=jpg&name=small']),
    media(first, '999', ['https://pbs.twimg.com/media/quoted.jpg']),
    media(first, '123', ['https://foreign.example/media/foreign.jpg', 'https://pbs.twimg.com/ext_tw_video_thumb/thumb.jpg']),
    media(first, '123', ['https://pbs.twimg.com/media/tagged_quote.jpg'], true),
    media(first, '', ['https://pbs.twimg.com/media/unbound.jpg']),
];
first.videos = [media(first, '999'), media(first, '', [], false, '999')];
second.photos = [media(second, '456', ['https://pbs.twimg.com/media/second.jpg'])];
second.videos = [media(second, '')];
const nested = {parentElement: {closest: () => first}, querySelector: () => {throw Error('Nested quote collected');}};
const primaryColumn = {querySelectorAll: () => [first, second, nested]};
global.document = {querySelector: selector => {
    if (selector !== 'main [data-testid="primaryColumn"]') throw Error('Unexpected document selector');
    return primaryColumn;
}};
console.log((new Function(`return (${source})();`))());
"""
    result = subprocess.run(["node", "-e", fixture], input=json.dumps(media_source), text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == [
        {"tweet_url": "https://x.com/poster/status/123", "photo_urls": ["https://pbs.twimg.com/media/own?format=jpg&name=small"], "has_video": False},
        {"tweet_url": "https://x.com/poster/status/456", "photo_urls": ["https://pbs.twimg.com/media/second.jpg"], "has_video": True},
    ]
