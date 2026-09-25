"""Collect liked tweet URLs from the logged-in X account."""

# Code version: v1.5.1-codex.0

from __future__ import annotations

import contextlib
import json
import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

from .browser.x_session import X_READY_SELECTORS, detect_account_handle, extract_account_handle_from_urlish
from .browser_sessions import (
    browser_descriptors,
    detect_safari_x_account_handle,
    extract_x_account_from_source,
    fetch_safari_page_snapshot,
    launch_chromium_context,
)
from .config import CrawlConfig
from .safari_automation import SafariContext
from .state import TaskState
from .x_text_history import XTextPost

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None
    PlaywrightTimeoutError = Exception
    PlaywrightError = Exception


X_HOME_URL = "https://x.com/home"
STATUS_URL_PATTERN = re.compile(r"^https://x\.com/([^/]+)/status/(\d+)")
INTERNAL_STATUS_URL_PATTERN = re.compile(r"^https://x\.com/(?:i|i/web)/status/(\d+)")
LIKES_REQUEST_TIMEOUT_SECONDS = 15
LIKES_API_RETRY_ATTEMPTS = 3
LIKES_API_RETRY_DELAY_SECONDS = 1.0
LIKES_API_HEADER_NAMES = {
    "authorization",
    "content-type",
    "dnt",
    "x-client-transaction-id",
    "x-csrf-token",
    "x-twitter-active-user",
    "x-twitter-auth-type",
    "x-twitter-client-language",
}
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LikesTimelineRequestTemplate:
    """Persist the request metadata needed to fetch more likes timeline pages."""

    api_url_base: str
    headers: dict[str, str]
    variables: dict[str, object]
    features: str
    field_toggles: str


def build_x_likes_url(account_handle: str) -> str:
    """Return the likes timeline URL for one X account handle."""
    cleaned_handle = (account_handle or "").strip().lstrip("@")
    if not cleaned_handle:
        raise RuntimeError("Could not build an X likes URL without a detected account handle.")
    return f"https://x.com/{cleaned_handle}/likes"


def ensure_playwright_available() -> None:
    """Raise a clear error when Playwright is not installed."""
    if sync_playwright is None:
        raise RuntimeError(
            "Playwright is not installed. Run `python3 -m pip install -r requirements.txt` "
            "and then `python3 -m playwright install chromium`."
        )


def clone_profile_for_playwright(config: CrawlConfig) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    """Copy the selected Chrome profile into a temporary user data dir."""
    source_user_data_dir = Path(config.chrome_user_data_dir).expanduser()
    source_profile_dir = source_user_data_dir / config.chrome_profile_directory

    if not source_profile_dir.exists():
        raise RuntimeError(f"Chrome profile directory was not found: {source_profile_dir}")

    temp_dir = tempfile.TemporaryDirectory(prefix="cachelikes-chrome-")
    temp_root = Path(temp_dir.name)
    target_user_data_dir = temp_root / "ChromeUserData"
    target_profile_dir = target_user_data_dir / config.chrome_profile_directory

    target_user_data_dir.mkdir(parents=True, exist_ok=True)

    local_state = source_user_data_dir / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, target_user_data_dir / "Local State")

    def ignore_transient_files(_directory: str, names: list[str]) -> set[str]:
        ignored = {
            "SingletonCookie",
            "SingletonLock",
            "SingletonSocket",
            "lockfile",
        }
        ignored.update(name for name in names if name.endswith(".lock"))
        return ignored

    shutil.copytree(source_profile_dir, target_profile_dir, dirs_exist_ok=True, ignore=ignore_transient_files)
    logger.info(
        "Cloned Chrome profile for Playwright.",
        extra={
            "source_profile_dir": str(source_profile_dir),
            "target_profile_dir": str(target_profile_dir),
        },
    )
    return target_user_data_dir, temp_dir


def wait_for_likes_page_ready(page) -> None:
    """Wait until the likes page looks usable or fail with a clear auth message."""
    auth_markers = [
        "Sign in",
        "Log in",
        "登录",
        "注册",
    ]

    deadline = time.time() + 30
    while time.time() < deadline:
        if any(page.locator(selector).count() for selector in X_READY_SELECTORS):
            page.wait_for_timeout(1_500)
            return

        body_text = page.locator("body").inner_text(timeout=5_000)
        if any(marker in body_text for marker in auth_markers):
            raise RuntimeError(
                "X did not appear logged in. Confirm the selected Chrome profile is signed in to X, "
                "and if Chrome is open, close its regular windows before retrying."
            )

        page.wait_for_timeout(1_000)

    raise RuntimeError("X likes page did not finish loading in time.")


def is_likes_timeline_response(response) -> bool:
    """Return whether one network response looks like the likes timeline GraphQL call."""
    response_url = str(getattr(response, "url", "") or "")
    parsed = urlparse(response_url)
    lowered_path = parsed.path.lower()
    if "/graphql/" not in lowered_path:
        return False
    return "/likes" in lowered_path


def normalize_status_url(url: str) -> str:
    """Collapse tweet detail links to a canonical X status URL."""
    candidate = (url or "").strip()
    if not candidate:
        return ""

    candidate = candidate.split("?", 1)[0].rstrip("/")
    if candidate.startswith("http://"):
        candidate = "https://" + candidate[len("http://") :]
    candidate = candidate.replace("https://twitter.com/", "https://x.com/")
    candidate = candidate.replace("https://www.x.com/", "https://x.com/")
    candidate = candidate.replace("https://www.twitter.com/", "https://x.com/")
    candidate = candidate.replace("https://mobile.x.com/", "https://x.com/")
    candidate = candidate.replace("https://mobile.twitter.com/", "https://x.com/")

    match = STATUS_URL_PATTERN.match(candidate)
    if match:
        handle, status_id = match.groups()
        return f"https://x.com/{handle}/status/{status_id}"

    internal_match = INTERNAL_STATUS_URL_PATTERN.match(candidate)
    if internal_match:
        return f"https://x.com/i/status/{internal_match.group(1)}"

    return ""


def extract_status_url_from_likes_entry(entry: dict[str, object]) -> str:
    """Build a canonical status URL from one likes timeline entry payload."""
    content = entry.get("content")
    if not isinstance(content, dict) or content.get("__typename") != "TimelineTimelineItem":
        return ""

    item_content = content.get("itemContent")
    if not isinstance(item_content, dict) or item_content.get("__typename") != "TimelineTweet":
        return ""

    tweet_result = item_content.get("tweet_results")
    if not isinstance(tweet_result, dict):
        return ""

    result = tweet_result.get("result")
    if not isinstance(result, dict):
        return ""

    if result.get("__typename") == "TweetWithVisibilityResults":
        nested_tweet = result.get("tweet")
        if isinstance(nested_tweet, dict):
            result = nested_tweet
        else:
            return ""

    legacy = result.get("legacy")
    if not isinstance(legacy, dict):
        return ""

    status_id = str(result.get("rest_id") or legacy.get("id_str") or legacy.get("conversation_id_str") or "").strip()
    if not status_id:
        return ""

    core = result.get("core")
    if not isinstance(core, dict):
        return f"https://x.com/i/status/{status_id}"

    user_results = core.get("user_results")
    if not isinstance(user_results, dict):
        return f"https://x.com/i/status/{status_id}"

    user_result = user_results.get("result")
    if not isinstance(user_result, dict):
        return f"https://x.com/i/status/{status_id}"

    user_legacy = user_result.get("legacy")
    screen_name = ""
    if isinstance(user_legacy, dict):
        screen_name = str(user_legacy.get("screen_name") or "").strip()
    if not screen_name:
        user_core = user_result.get("core")
        if isinstance(user_core, dict):
            screen_name = str(user_core.get("screen_name") or "").strip()
    if not screen_name:
        return f"https://x.com/i/status/{status_id}"

    return f"https://x.com/{screen_name}/status/{status_id}"


def parse_likes_timeline_page(payload: dict[str, object]) -> tuple[list[str], str]:
    """Extract canonical status URLs plus the next bottom cursor from one likes response."""
    instructions = extract_likes_timeline_instructions(payload)
    if not isinstance(instructions, list):
        return [], ""

    discovered_urls: list[str] = []
    bottom_cursor = ""

    for instruction in instructions:
        if not isinstance(instruction, dict):
            continue
        entries = instruction.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content")
            if isinstance(content, dict) and content.get("__typename") == "TimelineTimelineCursor":
                if content.get("cursorType") == "Bottom":
                    bottom_cursor = str(content.get("value") or "").strip()
                continue

            for entry_candidate in iter_likes_entry_candidates(entry):
                status_url = extract_status_url_from_likes_entry(entry_candidate)
                if status_url:
                    discovered_urls.append(status_url)

    return discovered_urls, bottom_cursor


def extract_likes_timeline_instructions(payload: dict[str, object]) -> list[dict[str, object]]:
    """Find the instructions list even if X renames one timeline wrapper layer."""
    candidate_paths = [
        ("data", "user", "result", "timeline", "timeline", "instructions"),
        ("data", "user", "result", "timeline_v2", "timeline", "instructions"),
        ("data", "user", "result", "timeline_response", "timeline", "instructions"),
        ("data", "user", "result", "timeline_response", "instructions"),
    ]
    for path_parts in candidate_paths:
        node: object = payload
        for part in path_parts:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(part)
        if isinstance(node, list):
            return node

    queue: list[object] = [payload]
    visited: set[int] = set()
    while queue:
        node = queue.pop(0)
        node_id = id(node)
        if node_id in visited:
            continue
        visited.add(node_id)

        if isinstance(node, dict):
            instructions = node.get("instructions")
            if isinstance(instructions, list) and any(
                isinstance(item, dict) and isinstance(item.get("entries"), list)
                for item in instructions
            ):
                return instructions
            queue.extend(value for value in node.values() if isinstance(value, (dict, list)))
        elif isinstance(node, list):
            queue.extend(value for value in node if isinstance(value, (dict, list)))

    return []


def iter_likes_entry_candidates(entry: dict[str, object]):
    """Yield one entry plus any nested module items that may contain tweets."""
    yield entry
    content = entry.get("content")
    if not isinstance(content, dict):
        return

    for key in ("items", "moduleItems"):
        items = content.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            nested_entry = item.get("item") if isinstance(item.get("item"), dict) else item
            if isinstance(nested_entry, dict):
                yield nested_entry


def build_likes_request_template(response, likes_url: str) -> LikesTimelineRequestTemplate:
    """Capture the authenticated request metadata for subsequent likes timeline pages."""
    parsed_url = urlparse(response.url)
    query_params = parse_qs(parsed_url.query)

    raw_variables = query_params.get("variables", [])
    raw_features = query_params.get("features", [])
    if not raw_variables or not raw_features:
        raise RuntimeError("X likes timeline request did not expose the expected pagination metadata.")

    request_headers = response.request.headers
    headers = {
        key: value
        for key, value in request_headers.items()
        if key.lower() in LIKES_API_HEADER_NAMES
    }
    headers["referer"] = likes_url

    return LikesTimelineRequestTemplate(
        api_url_base=f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}",
        headers=headers,
        variables=json.loads(raw_variables[0]),
        features=raw_features[0],
        field_toggles=query_params.get("fieldToggles", ["{}"])[0],
    )


def fetch_likes_timeline_page(page, template: LikesTimelineRequestTemplate, cursor: str | None) -> dict[str, object]:
    """Fetch one likes timeline page inside the authenticated browser session."""
    variables = dict(template.variables)
    if cursor:
        variables["cursor"] = cursor
    else:
        variables.pop("cursor", None)

    request_url = template.api_url_base + "?" + urlencode(
        {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": template.features,
            "fieldToggles": template.field_toggles,
        }
    )

    last_error: Exception | None = None
    body_text = ""
    status = 0

    for attempt_index in range(1, LIKES_API_RETRY_ATTEMPTS + 1):
        try:
            response = page.context.request.get(
                request_url,
                headers=template.headers,
                fail_on_status_code=False,
                timeout=120_000,
            )
        except PlaywrightError as exc:
            last_error = exc
            if attempt_index >= LIKES_API_RETRY_ATTEMPTS:
                raise RuntimeError(f"X likes timeline request aborted: {exc}") from exc

            logger.warning(
                "Retrying likes timeline request after a Playwright transport error.",
                extra={
                    "request_url": request_url,
                    "attempt": attempt_index,
                    "max_attempts": LIKES_API_RETRY_ATTEMPTS,
                    "error": str(exc),
                },
            )
            time.sleep(LIKES_API_RETRY_DELAY_SECONDS)
            continue

        status = response.status
        body_text = response.text()
        if status == 200:
            break

        if attempt_index >= LIKES_API_RETRY_ATTEMPTS:
            break

        logger.warning(
            "Retrying likes timeline request after a non-200 response.",
            extra={
                "request_url": request_url,
                "attempt": attempt_index,
                "max_attempts": LIKES_API_RETRY_ATTEMPTS,
                "status": status,
                "body_excerpt": body_text[:300],
            },
        )
        time.sleep(LIKES_API_RETRY_DELAY_SECONDS)

    if status != 200:
        if last_error is not None and not body_text:
            raise RuntimeError(f"X likes timeline request aborted: {last_error}") from last_error
        raise RuntimeError(f"X likes timeline request failed with HTTP {status}: {body_text[:300]}")

    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"X likes timeline response was not valid JSON: {body_text[:300]}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("X likes timeline response JSON did not contain an object payload.")
    return payload


def wait_for_initial_likes_timeline_response(page, response_box: dict[str, object]) -> object | None:
    """Wait briefly for X's own likes GraphQL request to complete."""
    deadline = time.time() + LIKES_REQUEST_TIMEOUT_SECONDS
    while time.time() < deadline:
        response = response_box.get("response")
        if response is not None:
            return response
        page.wait_for_timeout(500)
    return None


def collect_liked_tweet_urls_via_api(
    page,
    account_handle: str,
    likes_url: str,
    config: CrawlConfig,
    state: TaskState,
    initial_response,
) -> list[str]:
    """Page through the likes GraphQL timeline using the authenticated browser session."""
    initial_payload = json.loads(initial_response.text())
    template = build_likes_request_template(initial_response, likes_url)

    state.append_event("Switching to X likes timeline API pagination for reliable discovery.")
    logger.info(
        "Using likes timeline API pagination.",
        extra={
            "account_handle": account_handle,
            "likes_url": likes_url,
            "api_url_base": template.api_url_base,
        },
    )

    seen_urls: set[str] = set()
    cursor: str | None = None
    previous_cursor = ""
    stale_rounds = 0

    for round_index in range(config.max_scroll_rounds):
        payload = initial_payload if round_index == 0 else fetch_likes_timeline_page(page, template, cursor)
        page_urls, next_cursor = parse_likes_timeline_page(payload)

        before_count = len(seen_urls)
        seen_urls.update(page_urls)
        after_count = len(seen_urls)

        state.update(discovered_tweets=after_count, phase="collecting")
        state.append_event(
            f"Timeline page {round_index + 1}: discovered {after_count} unique liked tweet URLs."
        )
        logger.info(
            "Likes timeline page completed.",
            extra={
                "timeline_page": round_index + 1,
                "discovered_tweets": after_count,
                "newly_discovered_tweets": after_count - before_count,
                "stale_rounds": stale_rounds,
                "has_next_cursor": bool(next_cursor),
            },
        )

        if after_count == before_count:
            stale_rounds += 1
        else:
            stale_rounds = 0

        if not next_cursor or next_cursor == previous_cursor or stale_rounds >= config.stale_round_limit:
            break

        previous_cursor = next_cursor
        cursor = next_cursor
        time.sleep(config.scroll_pause_seconds)

    return sorted(seen_urls)


def collect_liked_tweet_urls_via_dom(page, config: CrawlConfig, state: TaskState) -> list[str]:
    """Fallback to DOM scrolling when the likes API request cannot be captured."""
    seen_urls: set[str] = set()
    stale_rounds = 0

    for round_index in range(config.max_scroll_rounds):
        links = page.locator('a[href*="/status/"], a[href*="/i/status/"]').evaluate_all(
            """(elements) => elements
            .map((element) => element.href)
            .filter((href) => href && href.includes("/status/"))"""
        )

        before_count = len(seen_urls)
        seen_urls.update(normalized_url for normalized_url in (normalize_status_url(link) for link in links) if normalized_url)
        after_count = len(seen_urls)

        state.update(discovered_tweets=after_count, phase="collecting")
        state.append_event(f"Scroll round {round_index + 1}: discovered {after_count} unique liked tweet URLs.")
        logger.info(
            "Scroll round completed.",
            extra={
                "scroll_round": round_index + 1,
                "discovered_tweets": after_count,
                "newly_discovered_tweets": after_count - before_count,
                "stale_rounds": stale_rounds,
            },
        )

        if after_count == before_count:
            stale_rounds += 1
        else:
            stale_rounds = 0

        if stale_rounds >= config.stale_round_limit:
            break

        page.mouse.wheel(0, 6000)
        with contextlib.suppress(Exception):
            page.evaluate(
                """() => {
                    window.scrollBy(0, Math.max(window.innerHeight, 6000));
                    const scrollables = Array.from(document.querySelectorAll('*')).filter(
                        (element) => element.scrollHeight > element.clientHeight + 24
                    );
                    scrollables.forEach((element) => {
                        element.scrollBy(0, Math.max(element.clientHeight, 4000));
                    });
                }"""
            )
        time.sleep(config.scroll_pause_seconds)

    return sorted(seen_urls)


def selected_x_browser_descriptor(config: CrawlConfig):
    """Resolve the browser descriptor requested for X collection."""
    descriptor = browser_descriptors(config).get(config.x_browser)
    if descriptor is None:
        raise RuntimeError(f"Unsupported X browser: {config.x_browser}")
    return descriptor


def collect_liked_tweet_urls_via_safari(
    account_handle: str,
    likes_url: str,
    config: CrawlConfig,
    state: TaskState,
    *,
    on_text_posts: Callable[[list[XTextPost]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[str]:
    """Collect liked URLs and visible post text inside signed-in Safari."""
    rounds = max(1, int(config.max_scroll_rounds))
    pause_seconds = max(0.2, float(config.scroll_pause_seconds))
    stale_limit = max(1, int(config.stale_round_limit))
    stop_requested = should_stop or (lambda: False)
    collect_links_js = """
() => {
    const primaryColumn = document.querySelector('main [data-testid="primaryColumn"]');
    if (!primaryColumn) return JSON.stringify([]);
    const hrefs = Array.from(primaryColumn.querySelectorAll('article[data-testid="tweet"]'))
        .filter((article) => !article.parentElement?.closest('article[data-testid="tweet"]'))
        .map((article) => article.querySelector('time')?.closest('a[href*="/status/"]')?.href)
        .filter(Boolean);
    return JSON.stringify(hrefs);
}
""".strip()
    collect_posts_js = """
() => {
    const primaryColumn = document.querySelector('main [data-testid="primaryColumn"]');
    if (!primaryColumn) return JSON.stringify([]);
    return JSON.stringify(Array.from(primaryColumn.querySelectorAll('article[data-testid="tweet"]'))
    .filter((article) => !article.parentElement?.closest('article[data-testid="tweet"]'))
    .map((article) => {
        const time = article.querySelector('time');
        const statusLink = time?.closest('a[href*="/status/"]');
        const textNode = article.querySelector('[data-testid="tweetText"]');
        if (!statusLink || !textNode) return null;
        const profileHref = article.querySelector('[data-testid="User-Name"] a[href]')
            ?.getAttribute('href') || '';
        const author = profileHref.match(/^\\/([A-Za-z0-9_]{1,15})(?:\\/|$)/);
        return {
            url: statusLink.href,
            content_text: String(textNode.innerText || textNode.textContent || '').trim(),
            author_handle: author ? author[1] : '',
            created_at: time.getAttribute('datetime') || '',
        };
    })
    .filter((post) => post && post.content_text));
}
""".strip()
    scroll_js = """
() => {
    window.scrollBy(0, Math.max(window.innerHeight, 6000));
    const scrollables = Array.from(document.querySelectorAll('*')).filter(
        (element) => element.scrollHeight > element.clientHeight + 24
    );
    scrollables.forEach((element) => {
        element.scrollBy(0, Math.max(element.clientHeight, 4000));
    });
    return 'scrolled';
}
""".strip()
    state.append_event(f"Launching a background Safari window for X likes collection at {likes_url}.")
    seen_urls: set[str] = set()
    seen_posts: dict[str, XTextPost] = {}
    pending_posts: dict[str, XTextPost] = {}
    stale_rounds = 0

    def flush_text_posts() -> None:
        if on_text_posts is None or not pending_posts:
            return
        on_text_posts(list(pending_posts.values()))
        pending_posts.clear()

    if stop_requested():
        return []
    with SafariContext(likes_url, lock_blocking=False) as context:
        page = context.primary_page
        page.wait_for_timeout(8_000)
        current_url = urlparse(str(page.url or ""))
        expected_url = urlparse(likes_url)
        if (
            current_url.hostname not in {"x.com", "www.x.com"}
            or current_url.path.rstrip("/").lower() != expected_url.path.rstrip("/").lower()
        ):
            raise RuntimeError("Safari did not open the authenticated X likes timeline.")
        for round_index in range(rounds):
            if stop_requested():
                break
            before_count = len(seen_urls)
            links_json = page.evaluate(collect_links_js)
            if isinstance(links_json, str):
                with contextlib.suppress(json.JSONDecodeError):
                    payload = json.loads(links_json)
                    if isinstance(payload, list):
                        seen_urls.update(
                            normalized
                            for normalized in (normalize_status_url(str(link)) for link in payload)
                            if normalized
                        )
            changed_text = False
            if on_text_posts is not None:
                posts_json = page.evaluate(collect_posts_js)
                if isinstance(posts_json, str):
                    with contextlib.suppress(json.JSONDecodeError):
                        posts = json.loads(posts_json)
                        if isinstance(posts, list):
                            for item in posts:
                                if not isinstance(item, dict):
                                    continue
                                url = normalize_status_url(str(item.get("url") or ""))
                                content_text = str(item.get("content_text") or "").strip()
                                if not url or not content_text:
                                    continue
                                post = XTextPost(
                                    url=url,
                                    content_text=content_text,
                                    author_handle=str(item.get("author_handle") or ""),
                                    created_at=str(item.get("created_at") or ""),
                                )
                                if seen_posts.get(url) != post:
                                    seen_posts[url] = post
                                    pending_posts[url] = post
                                    changed_text = True
                if len(pending_posts) >= 50:
                    flush_text_posts()
            state.update(discovered_tweets=len(seen_urls), phase="collecting")
            state.append_event(
                f"Safari likes round {round_index + 1}: found {len(seen_urls):,} unique post URLs."
            )
            stale_rounds = 0 if len(seen_urls) > before_count or changed_text else stale_rounds + 1
            if stale_rounds >= stale_limit or stop_requested() or round_index + 1 >= rounds:
                break
            page.evaluate(scroll_js)
            page.wait_for_timeout(int(pause_seconds * 1_000))
        flush_text_posts()
        final_url = page.url

    ordered_urls = sorted(seen_urls)
    state.update(account_name=account_handle, discovered_tweets=len(seen_urls), phase="collecting")
    state.append_event(
        f"Safari collected {len(seen_urls)} unique liked tweet URLs from {final_url.strip() or likes_url}."
    )
    return ordered_urls


def collect_liked_tweet_urls(
    config: CrawlConfig,
    state: TaskState,
    *,
    on_text_posts: Callable[[list[XTextPost]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[str, list[str]]:
    """Open X likes and return the account handle plus all discovered tweet URLs."""
    descriptor = selected_x_browser_descriptor(config)
    logger.info(
        "Collecting liked tweet URLs.",
        extra={
            "entry_url": X_HOME_URL,
            "browser": descriptor.browser_id,
            "browser_label": descriptor.label,
            "headless": config.headless,
            "max_scroll_rounds": config.max_scroll_rounds,
            "stale_round_limit": config.stale_round_limit,
        },
    )

    if descriptor.engine == "safari":
        account_handle = detect_safari_x_account_handle(wait_seconds=10)
        home_snapshot = {"url": "", "source": ""}
        if not account_handle:
            home_snapshot = fetch_safari_page_snapshot(X_HOME_URL, wait_seconds=10)
            account_handle = extract_account_handle_from_urlish(home_snapshot.get("url", ""))
        if not account_handle:
            account_handle = extract_x_account_from_source(home_snapshot.get("source", ""))
        if not account_handle:
            raise RuntimeError(
                "Could not detect the current X account handle from Safari. Open the signed-in X home page in Safari and try again."
            )
        likes_url = build_x_likes_url(account_handle)
        ordered_urls = collect_liked_tweet_urls_via_safari(
            account_handle,
            likes_url,
            config,
            state,
            on_text_posts=on_text_posts,
            should_stop=should_stop,
        )
        if not ordered_urls and not (should_stop and should_stop()):
            raise RuntimeError("No liked tweet URLs were found in Safari. The likes timeline may be empty or blocked.")
        return account_handle, ordered_urls

    ensure_playwright_available()
    with sync_playwright() as playwright:
        state.append_event(f"Launching an offscreen {descriptor.label} session for X likes collection.")
        with launch_chromium_context(
            playwright,
            descriptor,
            headless=config.headless,
            clone_profile_first=True,
            background_window=True,
        ) as context:
            page = context.pages[0] if context.pages else context.new_page()
            state.append_event(f"Opening X home {X_HOME_URL} in {descriptor.label}.")
            page.goto(X_HOME_URL, wait_until="domcontentloaded", timeout=120_000)
            wait_for_likes_page_ready(page)
            account_handle = detect_account_handle(page)
            likes_url = build_x_likes_url(account_handle)
            state.append_event(f"Opening likes page {likes_url}.")
            likes_response_box: dict[str, object] = {"response": None}

            def on_likes_response(response) -> None:
                if is_likes_timeline_response(response):
                    likes_response_box["response"] = response

            page.on("response", on_likes_response)
            page.goto(likes_url, wait_until="domcontentloaded", timeout=120_000)
            wait_for_likes_page_ready(page)

            state.update(account_name=account_handle)
            state.append_event(f"Ready to collect likes for @{account_handle} in {descriptor.label}.")
            logger.info(
                "Likes page ready.",
                extra={
                    "account_handle": account_handle,
                    "likes_url": likes_url,
                    "browser": descriptor.browser_id,
                },
            )
            initial_likes_response = wait_for_initial_likes_timeline_response(page, likes_response_box)
            if initial_likes_response is not None:
                ordered_urls = collect_liked_tweet_urls_via_api(
                    page=page,
                    account_handle=account_handle,
                    likes_url=likes_url,
                    config=config,
                    state=state,
                    initial_response=initial_likes_response,
                )
            else:
                state.append_event("Likes API request was not observed. Falling back to DOM scrolling.")
                logger.warning(
                    "Likes API request was not observed; falling back to DOM scrolling.",
                    extra={
                        "account_handle": account_handle,
                        "likes_url": likes_url,
                        "browser": descriptor.browser_id,
                    },
                )
                ordered_urls = collect_liked_tweet_urls_via_dom(page, config, state)

            if not ordered_urls:
                raise RuntimeError("No liked tweet URLs were found. The likes timeline may be empty or blocked.")

            logger.info(
                "Collected liked tweet URLs.",
                extra={
                    "account_handle": account_handle,
                    "discovered_tweets": len(ordered_urls),
                    "browser": descriptor.browser_id,
                },
            )
            return account_handle, ordered_urls
