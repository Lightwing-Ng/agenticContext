"""Focused tests for the persistent Agent source cache."""

# Code version: v2.2.0-codex.1

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock

from app.core.agent_source_cache import (
    AGENT_SOURCE_CACHE_TTL_SECONDS,
    AGENT_SOURCE_CACHE_RETRY_COOLDOWN_SECONDS,
    AgentSourceCache,
    AgentSourceCacheKey,
    agent_source_cache_path,
    get_or_collect_agent_source,
)
from app.core.resource_persistence import read_parquet_rows


class AgentSourceCacheTests(unittest.TestCase):
    """Validate cache reuse, refresh, and stale fallback behavior."""

    def test_fresh_payload_is_reused_without_collecting_again(self) -> None:
        with TemporaryDirectory() as raw_root:
            now = datetime(2026, 8, 15, 2, 0, tzinfo=timezone.utc)
            collector = Mock(
                return_value={
                    "platform": "gemini",
                    "recent_sessions": [{"id": "session-1"}],
                    "projects": [],
                }
            )
            first = get_or_collect_agent_source(
                local_store_root=raw_root,
                platform="gemini",
                browser="edge",
                source_kind="sources",
                collector=collector,
                now=now,
            )
            second = get_or_collect_agent_source(
                local_store_root=raw_root,
                platform="gemini",
                browser="edge",
                source_kind="sources",
                collector=collector,
                now=now + timedelta(minutes=5),
            )

        self.assertEqual(first["cache"]["status"], "miss")
        self.assertEqual(second["cache"]["status"], "hit")
        self.assertEqual(second["recent_sessions"], [{"id": "session-1"}])
        collector.assert_called_once()

    def test_store_seeds_memory_and_parquet_without_a_second_collection(self) -> None:
        with TemporaryDirectory() as raw_root:
            cache = AgentSourceCache(raw_root)
            cache.store(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                payload={"platform": "chatgpt", "recent_sessions": [{"id": "seeded"}]},
                now=datetime(2026, 8, 15, 2, 0, tzinfo=timezone.utc),
            )
            collector = Mock(return_value={"recent_sessions": [{"id": "unexpected"}]})
            response = AgentSourceCache(raw_root).get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                collector=collector,
                now=datetime(2026, 8, 15, 2, 1, tzinfo=timezone.utc),
            )

        self.assertEqual(response["recent_sessions"], [{"id": "seeded"}])
        self.assertEqual(response["cache"]["status"], "hit")
        self.assertEqual(response["cache"]["layer"], "parquet")
        collector.assert_not_called()

    def test_refresh_replaces_entry_and_failed_refresh_returns_stale_payload(self) -> None:
        with TemporaryDirectory() as raw_root:
            now = datetime(2026, 8, 15, 2, 0, tzinfo=timezone.utc)
            collector = Mock(return_value={"platform": "grok", "sessions": []})
            get_or_collect_agent_source(
                local_store_root=raw_root,
                platform="grok",
                browser="edge",
                source_kind="project-sessions",
                project_url="https://grok.com/project/project-1?tab=conversations",
                collector=collector,
                now=now,
            )
            collector.side_effect = RuntimeError("Edge is unavailable")
            stale = get_or_collect_agent_source(
                local_store_root=raw_root,
                platform="grok",
                browser="edge",
                source_kind="project-sessions",
                project_url="https://grok.com/project/project-1?tab=conversations",
                collector=collector,
                force_refresh=True,
                now=now + timedelta(seconds=1),
            )
            collector.side_effect = None
            collector.return_value = {"platform": "grok", "sessions": [{"id": "session-2"}]}
            refreshed = get_or_collect_agent_source(
                local_store_root=raw_root,
                platform="grok",
                browser="edge",
                source_kind="project-sessions",
                project_url="https://grok.com/project/project-1?tab=conversations",
                collector=collector,
                force_refresh=True,
                now=now + timedelta(minutes=20),
            )
            rows = read_parquet_rows(agent_source_cache_path(Path(raw_root)))

        self.assertEqual(stale["cache"]["status"], "stale")
        self.assertTrue(stale["cache"]["browser_check_required"])
        self.assertEqual(stale["sessions"], [])
        self.assertEqual(refreshed["cache"]["status"], "refreshed")
        self.assertEqual(refreshed["sessions"], [{"id": "session-2"}])
        self.assertEqual(len(rows or []), 1)
        self.assertEqual(collector.call_count, 3)

    def test_browser_and_project_are_isolated_cache_keys(self) -> None:
        with TemporaryDirectory() as raw_root:
            now = datetime(2026, 8, 15, 2, 0, tzinfo=timezone.utc)
            collector = Mock(
                side_effect=[
                    {"platform": "chatgpt", "recent_sessions": ["edge"]},
                    {"platform": "chatgpt", "recent_sessions": ["chrome"]},
                ]
            )
            edge = get_or_collect_agent_source(
                local_store_root=raw_root,
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                collector=collector,
                now=now,
            )
            chrome = get_or_collect_agent_source(
                local_store_root=raw_root,
                platform="chatgpt",
                browser="chrome",
                source_kind="sources",
                collector=collector,
                now=now,
            )

        self.assertEqual(edge["recent_sessions"], ["edge"])
        self.assertEqual(chrome["recent_sessions"], ["chrome"])
        self.assertEqual(collector.call_count, 2)
        self.assertGreater(AGENT_SOURCE_CACHE_TTL_SECONDS, 0)

    def test_canonical_key_merges_equivalent_project_urls(self) -> None:
        first = AgentSourceCacheKey.from_values(
            " Grok ",
            "EDGE",
            "Project-Sessions",
            "HTTPS://GROK.COM/project/project-1/?tab=conversations&view=all",
        )
        second = AgentSourceCacheKey.from_values(
            "grok",
            "edge",
            "project-sessions",
            "https://grok.com/project/project-1?view=all&tab=conversations",
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first.serialized,
            '["grok","edge","project-sessions","https://grok.com/project/project-1?tab=conversations&view=all",""]',
        )

    def test_cold_miss_is_coalesced_across_concurrent_requests(self) -> None:
        with TemporaryDirectory() as raw_root:
            cache = AgentSourceCache(raw_root)
            started = Event()
            release = Event()
            collector = Mock()

            def collect_once() -> dict[str, object]:
                started.set()
                release.wait(timeout=5)
                return {"platform": "gemini", "recent_sessions": [{"id": "one"}]}

            collector.side_effect = collect_once
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(
                        cache.get_or_collect,
                        platform="gemini",
                        browser="edge",
                        source_kind="sources",
                        collector=collector,
                    )
                    for _ in range(4)
                ]
                self.assertTrue(started.wait(timeout=5))
                release.set()
                results = [future.result(timeout=5) for future in futures]

        self.assertEqual(collector.call_count, 1)
        self.assertTrue(
            all(result["recent_sessions"] == [{"id": "one"}] for result in results)
        )

    def test_expired_read_returns_stale_while_one_background_refresh_runs(self) -> None:
        with TemporaryDirectory() as raw_root:
            cache = AgentSourceCache(raw_root, ttl_seconds=1)
            now = datetime.now(timezone.utc)
            refresh_started = Event()
            refresh_finished = Event()
            release = Event()
            call_count = 0

            def collect_with_delay() -> dict[str, object]:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    refresh_started.set()
                    release.wait(timeout=5)
                    refresh_finished.set()
                return {"platform": "grok", "recent_sessions": [{"id": str(call_count)}]}

            cache.get_or_collect(
                platform="grok",
                browser="edge",
                source_kind="sources",
                collector=collect_with_delay,
                now=now,
            )
            stale = cache.get_or_collect(
                platform="grok",
                browser="edge",
                source_kind="sources",
                collector=collect_with_delay,
                now=now + timedelta(seconds=2),
            )
            self.assertTrue(refresh_started.wait(timeout=5))
            release.set()
            self.assertTrue(refresh_finished.wait(timeout=5))
            with cache._condition:
                self.assertTrue(
                    cache._condition.wait_for(
                        lambda: not cache._refreshing,
                        timeout=5,
                    )
                )

        self.assertEqual(stale["cache"]["status"], "stale")
        self.assertTrue(stale["cache"]["refresh_in_progress"])
        self.assertEqual(call_count, 2)

    def test_passive_expired_read_preserves_stale_catalog_without_collecting(self) -> None:
        with TemporaryDirectory() as raw_root:
            cache = AgentSourceCache(raw_root, ttl_seconds=1)
            now = datetime(2026, 8, 15, 2, 0, tzinfo=timezone.utc)
            collector = Mock(
                return_value={"platform": "chatgpt", "recent_sessions": [{"id": "fresh"}]}
            )
            cache.get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="browser-session",
                collector=collector,
                now=now,
            )
            stale = cache.get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="browser-session",
                collector=collector,
                now=now + timedelta(seconds=2),
                stale_while_revalidate=False,
            )

        self.assertEqual(stale["cache"]["status"], "stale")
        self.assertFalse(stale["cache"]["refresh_in_progress"])
        self.assertEqual(stale["recent_sessions"], [{"id": "fresh"}])
        collector.assert_called_once()

    def test_passive_catalog_miss_returns_unprobed_without_collecting(self) -> None:
        with TemporaryDirectory() as raw_root:
            collector = Mock(return_value={"platform": "chatgpt", "recent_sessions": []})
            payload = AgentSourceCache(raw_root).get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                collector=collector,
                collect_on_miss=False,
                stale_while_revalidate=False,
            )

        self.assertEqual(payload["cache"]["status"], "unprobed")
        self.assertEqual(payload["cache"]["cached_at"], "")
        self.assertTrue(payload["cache"]["browser_check_required"])
        collector.assert_not_called()

    def test_new_cache_instance_reuses_parquet_as_l2(self) -> None:
        with TemporaryDirectory() as raw_root:
            now = datetime(2026, 8, 15, 2, 0, tzinfo=timezone.utc)
            first_cache = AgentSourceCache(raw_root)
            first_cache.get_or_collect(
                platform="grok",
                browser="edge",
                source_kind="sources",
                collector=lambda: {"platform": "grok", "recent_sessions": []},
                now=now,
            )
            collector = Mock(return_value={"platform": "grok", "recent_sessions": ["unexpected"]})
            second = AgentSourceCache(raw_root).get_or_collect(
                platform="grok",
                browser="edge",
                source_kind="sources",
                collector=collector,
                now=now + timedelta(minutes=1),
            )

        self.assertEqual(second["cache"]["status"], "hit")
        self.assertEqual(second["cache"]["layer"], "parquet")
        self.assertEqual(second["recent_sessions"], [])
        collector.assert_not_called()

    def test_failed_background_refresh_is_temporarily_coalesced(self) -> None:
        with TemporaryDirectory() as raw_root:
            cache = AgentSourceCache(raw_root, ttl_seconds=1)
            now = datetime.now(timezone.utc)
            failure_finished = Event()
            call_count = 0

            def fail_refresh() -> dict[str, object]:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    failure_finished.set()
                    raise RuntimeError("Edge is not signed in")
                return {"platform": "gemini", "recent_sessions": []}

            cache.get_or_collect(
                platform="gemini",
                browser="edge",
                source_kind="sources",
                collector=fail_refresh,
                now=now,
            )
            stale = cache.get_or_collect(
                platform="gemini",
                browser="edge",
                source_kind="sources",
                collector=fail_refresh,
                now=now + timedelta(seconds=2),
            )
            self.assertTrue(failure_finished.wait(timeout=5))
            suppressed = cache.get_or_collect(
                platform="gemini",
                browser="edge",
                source_kind="sources",
                collector=fail_refresh,
                now=now + timedelta(seconds=2, microseconds=1),
            )

        self.assertEqual(stale["cache"]["status"], "stale")
        self.assertEqual(suppressed["cache"]["status"], "stale")
        self.assertFalse(suppressed["cache"]["refresh_in_progress"])
        self.assertEqual(call_count, 2)
        self.assertGreater(AGENT_SOURCE_CACHE_RETRY_COOLDOWN_SECONDS, 0)


if __name__ == "__main__":
    unittest.main()
