"""Focused tests for the persistent Agent source cache."""

# Code version: v2.1.7-codex.1

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import Mock

from app.core.agent_source_cache import (
    AGENT_SOURCE_CACHE_MAX_CLOCK_SKEW_SECONDS,
    AGENT_SOURCE_CACHE_TTL_SECONDS,
    AGENT_SOURCE_CACHE_RETRY_COOLDOWN_SECONDS,
    AGENT_SOURCE_CACHE_SCHEMA,
    AGENT_SOURCE_CACHE_SCHEMA_VERSION,
    AgentSourceCache,
    AgentSourceCacheEntry,
    AgentSourceCacheKey,
    _with_cache_metadata,
    agent_source_cache_path,
    get_or_collect_agent_source,
)
from app.core.resource_persistence import read_parquet_rows, write_parquet_rows_atomic


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
            '["grok","edge","project-sessions","https://grok.com/project/project-1?tab=conversations&view=all"]',
        )

    def test_canonical_key_merges_chatgpt_project_slug_aliases(self) -> None:
        project_id = "g-p-6a978edb95308191a53d2bb113154c10"
        old_slug = AgentSourceCacheKey.from_values(
            "chatgpt",
            "edge",
            "project-sessions",
            f"https://chatgpt.com/g/{project_id}-antigravity/project",
        )
        current_slug = AgentSourceCacheKey.from_values(
            "CHATGPT",
            "EDGE",
            "PROJECT-SESSIONS",
            f"https://www.chatgpt.com/g/{project_id}-worthward/project/",
        )

        self.assertEqual(old_slug, current_slug)
        self.assertEqual(
            old_slug.project_url,
            f"https://chatgpt.com/g/{project_id}/project",
        )
        other_project = AgentSourceCacheKey.from_values(
            "chatgpt",
            "edge",
            "project-sessions",
            "https://chatgpt.com/g/g-p-11111111111111111111111111111111-other/project",
        )
        self.assertNotEqual(old_slug, other_project)

        insecure_alias = AgentSourceCacheKey.from_values(
            "chatgpt",
            "edge",
            "project-sessions",
            f"http://chatgpt.com/g/{project_id}-forged/project",
        )
        credentialed_alias = AgentSourceCacheKey.from_values(
            "chatgpt",
            "edge",
            "project-sessions",
            f"https://attacker@chatgpt.com/g/{project_id}/project",
        )
        custom_port_alias = AgentSourceCacheKey.from_values(
            "chatgpt",
            "edge",
            "project-sessions",
            f"https://chatgpt.com:444/g/{project_id}/project",
        )
        self.assertNotEqual(old_slug, insecure_alias)
        self.assertNotEqual(old_slug, credentialed_alias)
        self.assertNotEqual(old_slug, custom_port_alias)

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

    def test_bootstrap_store_supersedes_an_older_inflight_refresh(self) -> None:
        """Do not let an older browser flight roll back a newer bootstrap publication."""
        with TemporaryDirectory() as raw_root:
            cache = AgentSourceCache(raw_root)
            refresh_started = Event()
            release_refresh = Event()
            refresh_result: list[dict[str, object]] = []
            old_time = datetime(2026, 9, 9, 1, 0, tzinfo=timezone.utc)
            new_time = old_time + timedelta(minutes=1)

            def collect_old_catalog() -> dict[str, object]:
                refresh_started.set()
                self.assertTrue(release_refresh.wait(timeout=5))
                return {
                    "platform": "chatgpt",
                    "recent_sessions": [{"id": "old-flight"}],
                }

            refresh = Thread(
                target=lambda: refresh_result.append(
                    cache.get_or_collect(
                        platform="chatgpt",
                        browser="edge",
                        source_kind="sources",
                        collector=collect_old_catalog,
                        force_refresh=True,
                        now=old_time,
                    )
                )
            )
            refresh.start()
            self.assertTrue(refresh_started.wait(timeout=5))
            cache.store(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                payload={
                    "platform": "chatgpt",
                    "recent_sessions": [{"id": "new-bootstrap"}],
                },
                now=new_time,
            )
            release_refresh.set()
            refresh.join(timeout=5)
            self.assertFalse(refresh.is_alive())
            final = cache.get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                collector=lambda: {"unexpected": True},
                now=new_time,
            )

        self.assertEqual(
            refresh_result[0]["recent_sessions"],
            [{"id": "new-bootstrap"}],
        )
        self.assertEqual(final["recent_sessions"], [{"id": "new-bootstrap"}])
        self.assertEqual(final["cache"]["cached_at"], new_time.isoformat())
        self.assertEqual(cache._refreshing, set())

    def test_failed_inflight_refresh_superseded_by_store_returns_hit(self) -> None:
        with TemporaryDirectory() as raw_root:
            cache = AgentSourceCache(raw_root)
            refresh_started = Event()
            release_refresh = Event()
            refresh_result: list[dict[str, object]] = []

            def fail_old_refresh() -> dict[str, object]:
                refresh_started.set()
                self.assertTrue(release_refresh.wait(timeout=5))
                raise RuntimeError("old flight failed")

            refresh = Thread(
                target=lambda: refresh_result.append(
                    cache.get_or_collect(
                        platform="chatgpt",
                        browser="edge",
                        source_kind="sources",
                        collector=fail_old_refresh,
                        force_refresh=True,
                    )
                )
            )
            refresh.start()
            self.assertTrue(refresh_started.wait(timeout=5))
            try:
                cache.store(
                    platform="chatgpt",
                    browser="edge",
                    source_kind="sources",
                    payload={"recent_sessions": [{"id": "direct-store"}]},
                )
            finally:
                release_refresh.set()
                refresh.join(timeout=5)

        self.assertFalse(refresh.is_alive())
        self.assertEqual(len(refresh_result), 1)
        self.assertEqual(
            refresh_result[0]["recent_sessions"],
            [{"id": "direct-store"}],
        )
        self.assertEqual(refresh_result[0]["cache"]["status"], "hit")
        self.assertFalse(refresh_result[0]["cache"]["browser_check_required"])
        self.assertEqual(cache._refreshing, set())
        self.assertEqual(cache._refresh_failed_at, {})

    def test_successful_refresh_with_older_time_publishes_monotonically(self) -> None:
        with TemporaryDirectory() as raw_root:
            cache = AgentSourceCache(raw_root)
            initial_time = datetime.now(timezone.utc) - timedelta(hours=1)
            requested_time = initial_time - timedelta(minutes=1)
            cache.store(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                payload={"recent_sessions": [{"id": "initial"}]},
                now=initial_time,
            )
            collector = Mock(
                return_value={"recent_sessions": [{"id": "refreshed"}]}
            )

            result = cache.get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                collector=collector,
                force_refresh=True,
                now=requested_time,
            )

        self.assertEqual(result["cache"]["status"], "refreshed")
        self.assertEqual(result["recent_sessions"], [{"id": "refreshed"}])
        self.assertEqual(result["cache"]["cached_at"], initial_time.isoformat())
        collector.assert_called_once()

    def test_store_does_not_replace_a_newer_cache_timestamp(self) -> None:
        with TemporaryDirectory() as raw_root:
            cache = AgentSourceCache(raw_root)
            newer_time = datetime(2026, 9, 9, 1, 1, tzinfo=timezone.utc)
            cache.store(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                payload={"recent_sessions": [{"id": "newer"}]},
                now=newer_time,
            )
            cache.store(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                payload={"recent_sessions": [{"id": "older"}]},
                now=newer_time - timedelta(minutes=1),
            )
            result = cache.get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                collector=lambda: {"unexpected": True},
                now=newer_time,
            )

        self.assertEqual(result["recent_sessions"], [{"id": "newer"}])
        self.assertEqual(result["cache"]["cached_at"], newer_time.isoformat())

    def test_future_store_and_refresh_timestamps_do_not_pin_the_cache(self) -> None:
        with TemporaryDirectory() as raw_root:
            cache = AgentSourceCache(raw_root)
            future_time = datetime.now(timezone.utc) + timedelta(
                seconds=AGENT_SOURCE_CACHE_MAX_CLOCK_SKEW_SECONDS + 60,
            )
            cache.store(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                payload={"recent_sessions": [{"id": "future-store"}]},
                now=future_time,
            )
            collector = Mock(
                return_value={"recent_sessions": [{"id": "future-refresh"}]}
            )
            refreshed = cache.get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                collector=collector,
                force_refresh=True,
                now=future_time,
            )
            cache.store(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                payload={"recent_sessions": [{"id": "normal-store"}]},
            )
            final = cache.get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                collector=lambda: {"unexpected": True},
            )

        self.assertEqual(refreshed["cache"]["status"], "refreshed")
        self.assertEqual(
            refreshed["recent_sessions"],
            [{"id": "future-refresh"}],
        )
        self.assertLess(
            datetime.fromisoformat(refreshed["cache"]["cached_at"]),
            future_time,
        )
        self.assertEqual(final["recent_sessions"], [{"id": "normal-store"}])
        collector.assert_called_once()

    def test_existing_future_entry_does_not_block_corrected_store_or_refresh(
        self,
    ) -> None:
        with TemporaryDirectory() as raw_root:
            cache = AgentSourceCache(raw_root)
            key = AgentSourceCacheKey.from_values(
                "chatgpt",
                "edge",
                "sources",
            )
            with cache._condition:
                cache._catalog_loaded = True
                cache._entries[key] = AgentSourceCacheEntry(
                    payload={"recent_sessions": [{"id": "poisoned"}]},
                    cached_at=datetime.now(timezone.utc)
                    + timedelta(
                        seconds=AGENT_SOURCE_CACHE_MAX_CLOCK_SKEW_SECONDS + 60,
                    ),
                )
            cache.store(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                payload={"recent_sessions": [{"id": "corrected-store"}]},
            )
            stored = cache.get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                collector=lambda: {"unexpected": True},
            )
            with cache._condition:
                cache._entries[key] = AgentSourceCacheEntry(
                    payload={"recent_sessions": [{"id": "poisoned-again"}]},
                    cached_at=datetime.now(timezone.utc)
                    + timedelta(
                        seconds=AGENT_SOURCE_CACHE_MAX_CLOCK_SKEW_SECONDS + 60,
                    ),
                )
            collector = Mock(
                return_value={"recent_sessions": [{"id": "corrected"}]}
            )
            refreshed = cache.get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                collector=collector,
                force_refresh=True,
            )

        self.assertEqual(stored["recent_sessions"], [{"id": "corrected-store"}])
        self.assertEqual(refreshed["cache"]["status"], "refreshed")
        self.assertEqual(refreshed["recent_sessions"], [{"id": "corrected"}])
        collector.assert_called_once()

    def test_store_wakes_a_waiter_without_allowing_the_old_flight_to_overwrite(
        self,
    ) -> None:
        with TemporaryDirectory() as raw_root:
            cache = AgentSourceCache(raw_root)
            flight_started = Event()
            release_flight = Event()
            waiter_blocked = Event()
            old_result: list[dict[str, object]] = []
            waiter_result: list[dict[str, object]] = []

            def collect_old_catalog() -> dict[str, object]:
                flight_started.set()
                release_flight.wait(timeout=5)
                return {"recent_sessions": [{"id": "old-flight"}]}

            old_flight = Thread(
                target=lambda: old_result.append(
                    cache.get_or_collect(
                        platform="chatgpt",
                        browser="edge",
                        source_kind="sources",
                        collector=collect_old_catalog,
                    )
                )
            )
            old_flight.start()
            self.assertTrue(flight_started.wait(timeout=5))

            original_wait = cache._condition.wait

            def observed_wait(timeout: float | None = None) -> bool:
                waiter_blocked.set()
                return original_wait(timeout)

            cache._condition.wait = observed_wait
            waiter = Thread(
                target=lambda: waiter_result.append(
                    cache.get_or_collect(
                        platform="chatgpt",
                        browser="edge",
                        source_kind="sources",
                        collector=lambda: {"unexpected": True},
                    )
                )
            )
            waiter.start()
            self.assertTrue(waiter_blocked.wait(timeout=5))

            try:
                cache.store(
                    platform="chatgpt",
                    browser="edge",
                    source_kind="sources",
                    payload={"recent_sessions": [{"id": "direct-store"}]},
                )
                waiter.join(timeout=2)
                self.assertFalse(waiter.is_alive())
                self.assertTrue(old_flight.is_alive())
                self.assertEqual(
                    waiter_result[0]["recent_sessions"],
                    [{"id": "direct-store"}],
                )
            finally:
                release_flight.set()
                old_flight.join(timeout=5)
                waiter.join(timeout=5)

            self.assertFalse(old_flight.is_alive())
            self.assertEqual(
                old_result[0]["recent_sessions"],
                [{"id": "direct-store"}],
            )
            final = cache.get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                collector=lambda: {"unexpected": True},
            )

        self.assertEqual(final["recent_sessions"], [{"id": "direct-store"}])

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

    def test_future_parquet_row_is_discarded_without_a_cache_hit(self) -> None:
        with TemporaryDirectory() as raw_root:
            cache_path = agent_source_cache_path(Path(raw_root))
            payload = {"recent_sessions": [{"id": "future-row"}]}
            write_parquet_rows_atomic(
                cache_path,
                (
                    {
                        "schema_version": AGENT_SOURCE_CACHE_SCHEMA_VERSION,
                        "cache_key": '["chatgpt","edge","sources",""]',
                        "platform": "chatgpt",
                        "browser": "edge",
                        "source_kind": "sources",
                        "project_url": "",
                        "cached_at": datetime.max.replace(
                            tzinfo=timezone.utc
                        ).isoformat(),
                        "payload_json": json.dumps(payload),
                    },
                ),
                AGENT_SOURCE_CACHE_SCHEMA,
            )
            collector = Mock(
                return_value={"recent_sessions": [{"id": "collected"}]}
            )
            result = AgentSourceCache(raw_root).get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="sources",
                collector=collector,
            )

        self.assertEqual(result["cache"]["status"], "miss")
        self.assertEqual(result["recent_sessions"], [{"id": "collected"}])
        collector.assert_called_once()

    def test_cache_metadata_saturates_an_overflowing_expiration(self) -> None:
        maximum_time = datetime.max.replace(tzinfo=timezone.utc)

        result = _with_cache_metadata(
            {},
            status="hit",
            layer="memory",
            cached_at=maximum_time,
            now=maximum_time,
            ttl_seconds=AGENT_SOURCE_CACHE_TTL_SECONDS,
        )

        self.assertEqual(result["cache"]["cached_at"], maximum_time.isoformat())
        self.assertEqual(result["cache"]["expires_at"], maximum_time.isoformat())
        self.assertEqual(result["cache"]["age_seconds"], 0)

    def test_chatgpt_project_slug_alias_reuses_parquet_without_cross_project_leakage(
        self,
    ) -> None:
        project_id = "g-p-6a978edb95308191a53d2bb113154c10"
        old_url = f"https://chatgpt.com/g/{project_id}-antigravity/project"
        current_url = f"https://chatgpt.com/g/{project_id}-worthward/project"
        other_url = (
            "https://chatgpt.com/g/"
            "g-p-11111111111111111111111111111111-other/project"
        )
        cached_payload = {
            "platform": "chatgpt",
            "sessions": [{"id": "same-project-session"}],
        }
        superseded_payload = {
            "platform": "chatgpt",
            "sessions": [{"id": "superseded-session"}],
        }

        with TemporaryDirectory() as raw_root:
            cached_at = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)
            newer_alias_url = (
                f"https://www.chatgpt.com/g/{project_id}-newer-name/project/"
            )
            write_parquet_rows_atomic(
                agent_source_cache_path(Path(raw_root)),
                (
                    {
                        "schema_version": AGENT_SOURCE_CACHE_SCHEMA_VERSION,
                        "cache_key": json.dumps(
                            ["chatgpt", "edge", "project-sessions", old_url],
                            separators=(",", ":"),
                        ),
                        "platform": "chatgpt",
                        "browser": "edge",
                        "source_kind": "project-sessions",
                        "project_url": old_url,
                        "cached_at": cached_at.isoformat(),
                        "payload_json": json.dumps(superseded_payload),
                    },
                    {
                        "schema_version": AGENT_SOURCE_CACHE_SCHEMA_VERSION,
                        "cache_key": json.dumps(
                            [
                                "chatgpt",
                                "edge",
                                "project-sessions",
                                newer_alias_url,
                            ],
                            separators=(",", ":"),
                        ),
                        "platform": "chatgpt",
                        "browser": "edge",
                        "source_kind": "project-sessions",
                        "project_url": newer_alias_url,
                        "cached_at": (cached_at + timedelta(minutes=1)).isoformat(),
                        "payload_json": json.dumps(cached_payload),
                    },
                ),
                AGENT_SOURCE_CACHE_SCHEMA,
            )
            collector = Mock(return_value={"sessions": [{"id": "unexpected"}]})
            current = AgentSourceCache(raw_root).get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="project-sessions",
                project_url=current_url,
                collector=collector,
                now=cached_at + timedelta(minutes=2),
            )
            other = AgentSourceCache(raw_root).get_or_collect(
                platform="chatgpt",
                browser="edge",
                source_kind="project-sessions",
                project_url=other_url,
                collector=collector,
                collect_on_miss=False,
            )

        self.assertEqual(current["cache"]["status"], "hit")
        self.assertEqual(current["cache"]["layer"], "parquet")
        self.assertEqual(current["sessions"], [{"id": "same-project-session"}])
        self.assertEqual(other["cache"]["status"], "unprobed")
        self.assertEqual(other["cache"]["layer"], "none")
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
