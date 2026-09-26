"""Keep Safari's internal likes redirect bound to the authenticated account."""

# Code version: v1.0.0-codex.0

from __future__ import annotations

import json
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import CrawlConfig
from app.core.scraper import collect_liked_tweet_urls_via_safari
from app.core.state import TaskSnapshot, TaskState


def collect_with_page(page):
    context = MagicMock()
    context.primary_page = page
    context.__enter__.return_value = context
    with patch("app.core.scraper.SafariContext", return_value=context), patch(
        "app.core.scraper.collect_liked_tweet_urls_from_rendered_page", return_value=["post"]
    ) as collect:
        result = collect_liked_tweet_urls_via_safari(
            "signed_in", "https://x.com/signed_in/likes", CrawlConfig(x_browser="safari"),
            TaskState("test", snapshot_factory=TaskSnapshot),
        )
    collect.assert_called_once()
    return result


@pytest.mark.parametrize("url", ("https://x.com/signed_in/likes", "https://www.x.com/SIGNED_IN/likes/"))
def test_safari_exact_account_likes_path_remains_supported(url: str) -> None:
    page = MagicMock(url=url)
    assert collect_with_page(page) == ["post"]
    page.evaluate.assert_not_called()


@pytest.mark.parametrize("url", (
    "https://x.com/someone_else/likes",
    "https://x.com/i/timeline/likes",
    "https://x.com/i/likes",
    "https://foreign.example/i/history/likes",
    "https://www.x.com/i/history/likes",
    "http://x.com/i/history/likes",
    "https://x.com:8443/i/history/likes",
    "https://someone@x.com/i/history/likes",
))
def test_safari_rejects_unverified_likes_paths_before_reading_posts(url: str) -> None:
    page = MagicMock(url=url)
    with pytest.raises(RuntimeError, match="authenticated X likes timeline"):
        collect_with_page(page)
    page.evaluate.assert_not_called()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed to execute the redirect guard")
@pytest.mark.parametrize("profile,selected,dom_url,link_url,accepted", (
    ("https://x.com/signed_in", True, "https://x.com/i/history/likes", "https://x.com/i/history/likes", True),
    ("https://x.com/SIGNED_IN/", True, "https://x.com/i/history/likes/", "https://x.com/i/history/likes", True),
    ("https://x.com/someone_else", True, "https://x.com/i/history/likes", "https://x.com/i/history/likes", False),
    ("https://foreign.example/signed_in", True, "https://x.com/i/history/likes", "https://x.com/i/history/likes", False),
    (None, True, "https://x.com/i/history/likes", "https://x.com/i/history/likes", False),
    ("https://x.com/signed_in", False, "https://x.com/i/history/likes", "https://x.com/i/history/likes", False),
    ("https://x.com/signed_in", True, "https://foreign.example/i/history/likes", "https://x.com/i/history/likes", False),
    ("https://x.com/signed_in", True, "https://x.com/home", "https://x.com/i/history/likes", False),
    ("https://x.com/signed_in", True, "https://x.com/i/history/likes", "https://foreign.example/i/history/likes", False),
    ("https://x.com/signed_in", True, "https://x.com/i/history/likes", "https://x.com/someone_else/likes", False),
))
def test_safari_internal_history_requires_own_profile_and_selected_likes_navigation(
    profile: str | None, selected: bool, dom_url: str, link_url: str, accepted: bool,
) -> None:
    page = MagicMock(url="https://x.com/i/history/likes")

    def evaluate(expression, expected):
        fixture = json.dumps({"profile": profile, "selected": selected, "domUrl": dom_url, "linkUrl": link_url})
        script = f"""
const fixture = {fixture};
global.location = new URL(fixture.domUrl);
global.document = {{
    querySelector: selector => fixture.profile ? {{href: fixture.profile}} : null,
    querySelectorAll: selector => {{
        if (selector !== 'nav a[href]') throw new Error('Expected navigation-only links');
        return [{{href: fixture.linkUrl, getAttribute: name => name === 'aria-selected' && fixture.selected ? 'true' : null}}];
    }},
}};
const verify = {expression};
console.log(JSON.stringify(verify({json.dumps(expected)})));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    page.evaluate.side_effect = evaluate
    if accepted:
        assert collect_with_page(page) == ["post"]
    else:
        with pytest.raises(RuntimeError, match="authenticated X likes timeline"):
            collect_with_page(page)
    page.evaluate.assert_called_once()
