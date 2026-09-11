"""Background service for one complete Zhihu answer-cache run.

Code version: v0.4.2-codex.1
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any
from uuid import uuid4

from .config import BETA_STORE_ROOT, LOCAL_STORE_ROOT, CrawlConfig
from .job_lock import CacheTaskLock, SHARED_CACHE_TASK_LOCK
from .logging_setup import reset_job_id, set_job_id
from .state import TaskSnapshot, TaskState
from .zhihu_answers import (
    ZHIHU_EXAMPLE_PROFILE_URL,
    ZhihuAnswerStore,
    ZhihuArchiveError,
    normalize_zhihu_profile_url,
    sync_zhihu_answers,
    zhihu_archive_path,
)


logger = logging.getLogger(__name__)


class ZhihuTaskBusyError(RuntimeError):
    """A cache worker already owns the task slot requested by this service."""


def _beta_store_owns_cache_store(beta_store: Path, cache_store: Path) -> bool:
    """Reject a Beta root that could address the complete Local resources store."""

    return beta_store == cache_store or beta_store in cache_store.parents


def build_zhihu_answers_initial_snapshot(
    version: str,
    beta_store_root: Path | str = BETA_STORE_ROOT,
) -> TaskSnapshot:
    """Build an idle state without reading production or Beta cache content."""

    return TaskSnapshot(
        version=version,
        account_name="",
        output_dir=str(Path(beta_store_root) / "zhihu"),
        progress_unit="answers",
        message="Ready to cache a complete Zhihu answer collection.",
    )


def summarize_zhihu_answers_error_for_status(error: Exception) -> str:
    """Return a concise status error while the full traceback stays in logs."""

    text = str(error).strip()
    first_line = text.splitlines()[0] if text else error.__class__.__name__
    return first_line if len(first_line) <= 500 else f"{first_line[:497]}..."


class ZhihuAnswersService:
    """Own one cooperative worker and expose bounded archive reads."""

    def __init__(
        self,
        state: TaskState,
        beta_store_root: Path | str = BETA_STORE_ROOT,
        cache_store_root: Path | str = LOCAL_STORE_ROOT,
        task_lock: CacheTaskLock | None = None,
    ) -> None:
        self._state = state
        self._configured_beta_store_root = Path(beta_store_root).expanduser().absolute()
        self._configured_cache_store_root = Path(cache_store_root).expanduser().absolute()
        self._beta_store_root = self._configured_beta_store_root.resolve(strict=False)
        resolved_cache_store = self._configured_cache_store_root.resolve(strict=False)
        self._cache_store_root = resolved_cache_store
        production_cache_store = LOCAL_STORE_ROOT.expanduser().resolve(strict=False)
        if _beta_store_owns_cache_store(self._beta_store_root, resolved_cache_store):
            raise ValueError(
                "The Beta archive root must not own or equal the Local resources store."
            )
        self._task_lock = task_lock or (
            SHARED_CACHE_TASK_LOCK
            if resolved_cache_store == production_cache_store
            else CacheTaskLock(resolved_cache_store / ".cache_task.lock")
        )
        self._lifecycle_lock = RLock()
        self._summary_lock = RLock()
        self._summary_cache: dict[
            str,
            tuple[tuple[int, int, int, int] | None, dict[str, Any], str],
        ] = {}
        self._stop_requested = Event()
        self._worker: Thread | None = None
        self._owns_task_lock = False
        self._commit_started = False
        self._config = CrawlConfig()
        self._profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
        self._browser_id = "edge"

    def _assert_store_roots_stable(self) -> None:
        """Reject root aliases or rebinding before any Beta archive write."""

        try:
            current_beta_store = self._configured_beta_store_root.resolve(strict=False)
            current_cache_store = self._configured_cache_store_root.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ZhihuArchiveError(
                "Zhihu answer storage roots could not be verified."
            ) from exc
        if (
            self._configured_beta_store_root.is_symlink()
            or current_beta_store != self._beta_store_root
            or current_cache_store != self._cache_store_root
            or _beta_store_owns_cache_store(current_beta_store, current_cache_store)
        ):
            raise ZhihuArchiveError(
                "Zhihu answer storage roots changed after service initialization."
            )

    def is_running(self) -> bool:
        """Return whether this service has an active worker."""

        return bool(self._state.snapshot()["running"])

    def browse_answers(
        self,
        profile_url: str,
        *,
        query: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Return one bounded page of local answer summaries."""

        self._assert_store_roots_stable()
        profile = normalize_zhihu_profile_url(profile_url)
        store = ZhihuAnswerStore(
            self._configured_beta_store_root,
            profile,
            expected_beta_store_root=self._beta_store_root,
        )
        return store.browse(query=query, page=page, page_size=page_size)

    def read_answer(self, profile_url: str, answer_id: str) -> dict[str, Any] | None:
        """Return one complete answer from the local archive without remote access."""

        self._assert_store_roots_stable()
        profile = normalize_zhihu_profile_url(profile_url)
        store = ZhihuAnswerStore(
            self._configured_beta_store_root,
            profile,
            expected_beta_store_root=self._beta_store_root,
        )
        return store.read_answer(answer_id)

    def start(self, config: CrawlConfig, profile_url: str, browser_id: str) -> None:
        """Validate and start one complete answer-cache run."""

        self._assert_store_roots_stable()
        profile = normalize_zhihu_profile_url(profile_url)
        normalized_browser = str(browser_id or "").strip().casefold()
        from .browser_sessions import browser_descriptors

        descriptor = browser_descriptors(config).get(normalized_browser)
        if descriptor is None or descriptor.engine != "chromium":
            raise ValueError("Zhihu answer caching requires Chrome or Edge.")
        output_dir = str(
            zhihu_archive_path(
                self._configured_beta_store_root,
                profile,
                expected_beta_store_root=self._beta_store_root,
            ).parent
        )

        with self._lifecycle_lock:
            if self.is_running():
                raise ZhihuTaskBusyError("A Zhihu answer cache is already running.")
            if not self._task_lock.acquire("beta-zhihu-answers-cache"):
                raise ZhihuTaskBusyError(
                    "A cache task is already running in another window or browser. "
                    "Stop it there before starting the Zhihu answer cache."
                )
            self._owns_task_lock = True
            try:
                confirmed_output_dir = str(
                    zhihu_archive_path(
                        self._configured_beta_store_root,
                        profile,
                        expected_beta_store_root=self._beta_store_root,
                    ).parent
                )
                if confirmed_output_dir != output_dir:
                    raise RuntimeError("Zhihu answer storage path changed during task admission.")
                self._profile = profile
                self._browser_id = normalized_browser
                self._config = config
                self._stop_requested.clear()
                self._commit_started = False
                self._state.reset_for_run()
                self._state.update(
                    account_name=profile.author_token,
                    output_dir=output_dir,
                    progress_unit="answers",
                    message=f"Preparing {profile.author_token}'s Zhihu answer cache...",
                )
                self._worker = Thread(target=self._run, daemon=True)
                self._worker.start()
            except Exception as exc:
                try:
                    self._state.finish_error(summarize_zhihu_answers_error_for_status(exc))
                finally:
                    if self._owns_task_lock:
                        self._owns_task_lock = False
                        self._task_lock.release()
                raise

    def request_stop(self) -> bool:
        """Request a cooperative stop without replacing the last complete archive."""

        with self._lifecycle_lock:
            snapshot = self._state.snapshot()
            if not bool(snapshot.get("running")):
                return False
            if self._commit_started:
                self._state.append_event(
                    "The verified archive commit has already started and will finish atomically."
                )
                return False
            self._stop_requested.set()
            self._state.update(phase="stopping")
            self._state.append_event(
                "Stop requested. The previous complete Zhihu archive will remain available."
            )
            return True

    def _is_stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def _begin_commit(self) -> bool:
        """Atomically choose between an accepted Stop and the final archive commit."""

        with self._lifecycle_lock:
            self._assert_store_roots_stable()
            if self._stop_requested.is_set():
                return False
            self._commit_started = True
            self._state.update(
                phase="committing",
                message="Writing and verifying the complete Zhihu answer archive...",
            )
            return True

    def _apply_result_metrics(self, result: dict[str, Any]) -> None:
        snapshot = self._state.snapshot()
        metrics = dict(snapshot.get("performance_metrics") or {})
        for key in (
            "expected_answers",
            "pages_processed",
            "duplicates",
            "unavailable_answers",
            "raw_answers",
            "stability_passes",
            "added",
            "changed",
            "removed",
            "unchanged",
        ):
            metrics[key] = result.get(key)
        self._state.update(
            discovered_tweets=int(result.get("expected_answers") or result.get("processed_answers") or 0),
            queued_tweets=int(result.get("processed_answers") or 0),
            processed_tweets=int(result.get("processed_answers") or 0),
            downloaded_posts=int(result.get("cached_answers") or 0),
            downloaded_tweets=int(result.get("cached_answers") or 0),
            performance_metrics=metrics,
        )

    def _run(self) -> None:
        """Execute the browser-backed collection while owning the shared task lock."""

        job_id = uuid4().hex[:12]
        token = set_job_id(job_id)
        try:
            logger.info(
                "Zhihu answer cache started.",
                extra={
                    "job_id": job_id,
                    "author_token": self._profile.author_token,
                    "browser": self._browser_id,
                },
            )
            if self._is_stop_requested():
                with self._lifecycle_lock:
                    self._state.finish_stopped(
                        "Zhihu answer cache stopped before the browser was launched. "
                        "The previous complete archive was preserved."
                    )
                return
            result = sync_zhihu_answers(
                self._state,
                self._config,
                self._profile.answers_url,
                self._browser_id,
                self._is_stop_requested,
                beta_store_root=self._configured_beta_store_root,
                begin_commit=self._begin_commit,
                expected_beta_store_root=self._beta_store_root,
            )
            self._apply_result_metrics(result)
            if bool(result.get("stopped")):
                with self._lifecycle_lock:
                    self._state.finish_stopped(
                        f"Stopped after reading {int(result.get('processed_answers') or 0):,} answers. "
                        "The previous complete archive was preserved."
                    )
                return
            with self._lifecycle_lock:
                unavailable = int(result.get("unavailable_answers") or 0)
                completion_message = (
                    f"Finished caching {int(result.get('cached_answers') or 0):,} verified unique "
                    f"answer records across {int(result.get('pages_processed') or 0):,} pages."
                )
                if unavailable:
                    completion_message += (
                        f" Zhihu reports {int(result.get('expected_answers') or 0):,}; "
                        f"{unavailable:,} are not enumerable through its answer pagination."
                    )
                self._state.finish_success(completion_message)
            logger.info("Zhihu answer cache finished successfully.", extra={"job_id": job_id, **result})
        except Exception as exc:  # pragma: no cover - live browser failures are environment-specific
            with self._lifecycle_lock:
                if self._stop_requested.is_set() and not self._commit_started:
                    self._state.finish_stopped(
                        "Zhihu answer cache stopped. The previous complete archive was preserved."
                    )
                else:
                    self._state.finish_error(summarize_zhihu_answers_error_for_status(exc))
            logger.exception(
                "Zhihu answer cache failed.",
                extra={"job_id": job_id, "error": str(exc)},
            )
        finally:
            reset_job_id(token)
            with self._lifecycle_lock:
                if self._owns_task_lock:
                    self._owns_task_lock = False
                    self._task_lock.release()
                self._commit_started = False

    def snapshot(self, profile_url: str | None = None) -> dict[str, Any]:
        """Return UI status plus a bounded readback of one persisted archive."""

        with self._lifecycle_lock:
            state = self._state.snapshot()
            state_profile = self._profile
            active_profile = state_profile
            browser_id = self._browser_id
        if not bool(state.get("running")) and profile_url is not None:
            active_profile = normalize_zhihu_profile_url(profile_url)
        state_matches_profile = active_profile.author_token == state_profile.author_token
        if not state_matches_profile:
            state = {
                **state,
                "phase": "idle",
                "message": "Ready to cache a complete Zhihu answer collection.",
                "last_error": "",
                "processed_tweets": 0,
                "performance_metrics": {},
            }

        output_dir = str(self._beta_store_root / "zhihu" / active_profile.author_token)
        store_summary: dict[str, Any] = {
            "cache_exists": False,
            "cached_answers": 0,
            "expected_answers": None,
            "unavailable_answers": 0,
            "pages_processed": 0,
            "raw_answers": 0,
            "duplicates": 0,
            "stability_passes": 0,
            "author_name": "",
            "output_dir": output_dir,
            "recent_answers": [],
        }
        archive_location_error = ""
        try:
            archive_path = zhihu_archive_path(
                self._configured_beta_store_root,
                active_profile,
                expected_beta_store_root=self._beta_store_root,
            )
            stat = archive_path.stat()
            archive_signature: tuple[int, int, int, int] | None = (
                stat.st_dev,
                stat.st_ino,
                stat.st_mtime_ns,
                stat.st_size,
            )
        except FileNotFoundError:
            archive_signature = None
        except RuntimeError as exc:
            archive_signature = None
            archive_location_error = summarize_zhihu_answers_error_for_status(exc)
        except OSError as exc:
            archive_signature = None
            archive_location_error = summarize_zhihu_answers_error_for_status(exc)
        with self._summary_lock:
            cached = None if archive_location_error else self._summary_cache.get(
                active_profile.author_token
            )
            if cached is not None and cached[0] == archive_signature:
                store_summary = cached[1]
                archive_error = cached[2]
            elif archive_location_error:
                archive_error = archive_location_error
            else:
                archive_error = ""
                try:
                    store_summary = ZhihuAnswerStore.load_summary(
                        self._configured_beta_store_root,
                        active_profile,
                        expected_beta_store_root=self._beta_store_root,
                    )
                except RuntimeError as exc:
                    archive_error = summarize_zhihu_answers_error_for_status(exc)
                self._summary_cache[active_profile.author_token] = (
                    archive_signature,
                    store_summary,
                    archive_error,
                )
                if len(self._summary_cache) > 16:
                    self._summary_cache.pop(next(iter(self._summary_cache)))

        metrics = dict(state.get("performance_metrics") or {})

        def metric(name: str, default: Any = 0) -> Any:
            if name in metrics:
                return metrics[name]
            return 0 if metrics else default

        processed_default = (
            store_summary.get("cached_answers")
            if not bool(state.get("running")) and not metrics
            else 0
        )

        return {
            "running": bool(state.get("running")),
            "phase": str(state.get("phase") or "idle"),
            "message": str(state.get("message") or ""),
            "profile_url": active_profile.answers_url,
            "author_token": active_profile.author_token,
            "author_name": str(store_summary.get("author_name") or ""),
            "browser": browser_id,
            "expected_answers": metric(
                "expected_answers",
                store_summary.get("expected_answers"),
            ),
            "processed_answers": int(
                state.get("processed_tweets") or processed_default or 0
            ),
            "cached_answers": int(store_summary.get("cached_answers") or 0),
            "pages_processed": int(
                metric("pages_processed", store_summary.get("pages_processed") or 0) or 0
            ),
            "duplicates": int(
                metric("duplicates", store_summary.get("duplicates") or 0) or 0
            ),
            "unavailable_answers": int(
                metric(
                    "unavailable_answers",
                    store_summary.get("unavailable_answers") or 0,
                )
                or 0
            ),
            "raw_answers": int(
                metric("raw_answers", store_summary.get("raw_answers") or 0) or 0
            ),
            "stability_passes": int(
                metric(
                    "stability_passes",
                    store_summary.get("stability_passes") or 0,
                )
                or 0
            ),
            "added": int(metrics.get("added") or 0),
            "changed": int(metrics.get("changed") or 0),
            "removed": int(metrics.get("removed") or 0),
            "unchanged": int(metrics.get("unchanged") or 0),
            "cache_exists": bool(store_summary.get("cache_exists")),
            "output_dir": str(store_summary.get("output_dir") or output_dir),
            "last_error": str(state.get("last_error") or archive_error),
            "recent_answers": list(store_summary.get("recent_answers") or []),
        }
