"""Orchestration service for the cache job."""

# Code version: v1.6.1-codex.1

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import logging
from threading import Thread

from .cache_service_support import CooperativeCacheWorker, append_shadow_backup_completion
from .config import CrawlConfig, LOCAL_STORE_ROOT, X_LOCAL_STORE_DIRNAME
from .downloader import DownloadResult, LocalTweetCacheIndex, download_tweet_media
from .job_lock import CacheTaskLock
from .scraper import collect_liked_tweet_urls
from .shadow_backup import ShadowBackupService
from .state import TaskState

logger = logging.getLogger(__name__)


class CacheLikesService(CooperativeCacheWorker):
    """Manage a single background cache job."""

    def __init__(
        self,
        state: TaskState,
        task_lock: CacheTaskLock | None = None,
        shadow_backup_service: ShadowBackupService | None = None,
    ) -> None:
        super().__init__(state, task_lock)
        self._shadow_backup_service = shadow_backup_service

    def start(self, config: CrawlConfig) -> None:
        """Start a new background job."""
        self._start_worker(
            lock_owner="x-cache",
            already_running_message="A cache job is already running.",
            lock_busy_message=(
                "A cache task is already running in another Cache Likes window or browser. "
                "Stop it there before starting a new task."
            ),
            target=self._run,
            args=(config,),
            thread_factory=Thread,
        )

    def request_stop(self) -> bool:
        """Request cooperative stop for the active job."""
        return self._request_stop(
            "Emergency stop requested. Waiting for current task to stop."
        )

    def _run(self, config: CrawlConfig) -> None:
        """Execute the full job pipeline."""
        job_id, token = self._begin_worker_job()
        try:
            logger.info(
                "Cache job started.",
                extra={
                    "job_id": job_id,
                    "x_browser": config.x_browser,
                    "chrome_profile_directory": config.chrome_profile_directory,
                    "chrome_user_data_dir": str(config.chrome_user_data_dir),
                    "headless": config.headless,
                    "download_workers": config.download_workers,
                    "max_media_items": config.max_media_items,
                    "max_scroll_rounds": config.max_scroll_rounds,
                    "scroll_pause_seconds": config.scroll_pause_seconds,
                    "stale_round_limit": config.stale_round_limit,
                },
            )
            if self._is_stop_requested():
                self._state.finish_stopped("Job stopped before collection started.")
                return

            account_handle, tweet_urls = collect_liked_tweet_urls(config, self._state)
            tweet_urls = list(dict.fromkeys(tweet_urls))
            account_name = config.sanitized_account_name(account_handle)
            output_dir = LOCAL_STORE_ROOT / X_LOCAL_STORE_DIRNAME
            cache_index = LocalTweetCacheIndex.build(output_dir)
            logger.info(
                "Likes collection completed.",
                extra={
                    "job_id": job_id,
                    "account_handle": account_handle,
                    "detected_account_name": account_name,
                    "cache_namespace": X_LOCAL_STORE_DIRNAME,
                    "discovered_tweets": len(tweet_urls),
                    "output_dir": str(output_dir),
                },
            )

            self._state.update(output_dir=str(output_dir), phase="downloading", discovered_tweets=len(tweet_urls))
            self._state.append_event(
                f"Starting media download with {config.download_workers} worker(s) for up to {config.max_media_items} files from "
                f"{len(tweet_urls)} liked tweets into {output_dir}."
            )
            if config.download_workers > 1:
                self._state.append_event(
                    "Parallel mode is enabled. The media cap is treated as a soft ceiling so one tweet is never partially cached."
                )

            downloaded_media = 0
            downloaded_posts = 0
            downloaded_images = 0
            downloaded_videos = 0
            skipped = 0
            failed = 0
            oversized_media = 0
            futures: dict[Future[DownloadResult], tuple[int, str]] = {}
            next_index = 0
            stop_requested = False
            cap_announced = False
            stop_wait_announced = False

            def update_progress_snapshot() -> None:
                self._state.update(
                    downloaded_tweets=downloaded_media,
                    downloaded_posts=downloaded_posts,
                    downloaded_images=downloaded_images,
                    downloaded_videos=downloaded_videos,
                    skipped_tweets=skipped,
                    failed_tweets=failed,
                )

            with ThreadPoolExecutor(max_workers=max(1, config.download_workers), thread_name_prefix="x-download") as executor:
                while next_index < len(tweet_urls) or futures:
                    if self._is_stop_requested():
                        stop_requested = True
                        if not stop_wait_announced:
                            self._state.append_event(
                                "Stop requested. No new tweets will be queued. Waiting for active download workers to finish."
                            )
                            stop_wait_announced = True

                    if not stop_requested:
                        while next_index < len(tweet_urls) and len(futures) < max(1, config.download_workers):
                            remaining_media_items = config.max_media_items - downloaded_media
                            if config.download_workers > 1:
                                remaining_media_items -= len(futures)

                            if remaining_media_items <= 0:
                                if not cap_announced:
                                    self._state.append_event(
                                        f"Reached the temporary test cap of about {config.max_media_items} media files."
                                    )
                                    cap_announced = True
                                break

                            tweet_index = next_index + 1
                            tweet_url = tweet_urls[next_index]
                            next_index += 1

                            worker_media_budget = remaining_media_items if config.download_workers == 1 else None
                            self._state.append_event(f"Queued liked tweet {tweet_index}/{len(tweet_urls)}")
                            logger.info(
                                "Queued liked tweet for download.",
                                extra={
                                    "job_id": job_id,
                                    "tweet_url": tweet_url,
                                    "tweet_index": tweet_index,
                                    "tweet_total": len(tweet_urls),
                                    "remaining_media_items": worker_media_budget,
                                    "active_workers": len(futures),
                                },
                            )
                            futures[
                                executor.submit(
                                    download_tweet_media,
                                    tweet_url,
                                    output_dir,
                                    config,
                                    self._state,
                                    remaining_media_items=worker_media_budget,
                                    cache_index=cache_index,
                                )
                            ] = (tweet_index, tweet_url)

                    if not futures:
                        break

                    done, _pending = wait(set(futures), return_when=FIRST_COMPLETED)
                    for future in done:
                        index, tweet_url = futures.pop(future)
                        try:
                            result = future.result()
                            if result.skipped:
                                skipped += 1
                                oversized_media += result.skipped_oversized_media_count
                                logger.info(
                                    "Tweet skipped.",
                                    extra={
                                        "job_id": job_id,
                                        "tweet_url": tweet_url,
                                        "tweet_index": index,
                                        "skipped_tweets": skipped,
                                    },
                                )
                            else:
                                downloaded_post_increment = (
                                    result.downloaded_post_count
                                    if result.downloaded_post_count > 0
                                    else (1 if result.downloaded_media_count > 0 else 0)
                                )
                                downloaded_media += result.downloaded_media_count
                                downloaded_posts += downloaded_post_increment
                                downloaded_images += result.downloaded_image_count
                                downloaded_videos += result.downloaded_video_count
                                oversized_media += result.skipped_oversized_media_count
                                logger.info(
                                    "Tweet download completed.",
                                    extra={
                                        "job_id": job_id,
                                        "tweet_url": tweet_url,
                                        "tweet_index": index,
                                        "downloaded_media_count": result.downloaded_media_count,
                                        "downloaded_media_total": downloaded_media,
                                        "downloaded_post_increment": downloaded_post_increment,
                                        "downloaded_posts_total": downloaded_posts,
                                        "downloaded_images_total": downloaded_images,
                                        "downloaded_videos_total": downloaded_videos,
                                    },
                                )
                        except Exception as exc:  # pragma: no cover
                            failed += 1
                            self._state.append_event(str(exc))
                            logger.exception(
                                "Tweet download failed.",
                                extra={
                                    "job_id": job_id,
                                    "tweet_url": tweet_url,
                                    "tweet_index": index,
                                    "failed_tweets": failed,
                                },
                            )

                        update_progress_snapshot()

                    if cap_announced and not futures:
                        break

            # A worker may observe an emergency stop after the final scheduling pass.
            # Re-check here so a fully drained executor cannot mask that request as success.
            stop_requested = stop_requested or self._is_stop_requested()
            if stop_requested:
                self._state.finish_stopped(
                    f"Emergency stop completed. Downloaded {downloaded_posts} posts, {downloaded_images} images, "
                    f"{downloaded_videos} videos ({downloaded_media} media files), "
                    f"skipped {skipped}, failed {failed}."
                )
                logger.info(
                    "Cache job stopped by operator.",
                    extra={
                        "job_id": job_id,
                        "downloaded_media_total": downloaded_media,
                        "downloaded_posts_total": downloaded_posts,
                        "downloaded_images_total": downloaded_images,
                        "downloaded_videos_total": downloaded_videos,
                        "skipped_tweets": skipped,
                        "failed_tweets": failed,
                    },
                )
                return

            completion_message = (
                f"Finished. Discovered {len(tweet_urls)} posts, downloaded {downloaded_media} media files "
                f"across {downloaded_posts} posts ({downloaded_images} images, {downloaded_videos} videos), "
                f"skipped {skipped}, exceeded the size limit {oversized_media}, failed {failed}."
            )
            completion_message = append_shadow_backup_completion(
                completion_message,
                shadow_backup_service=self._shadow_backup_service,
                state=self._state,
                config=config,
            )
            self._state.finish_success(completion_message)
            logger.info(
                "Cache job finished successfully.",
                extra={
                    "job_id": job_id,
                    "discovered_tweets": len(tweet_urls),
                    "downloaded_media_total": downloaded_media,
                    "downloaded_posts_total": downloaded_posts,
                    "downloaded_images_total": downloaded_images,
                    "downloaded_videos_total": downloaded_videos,
                    "skipped_tweets": skipped,
                    "failed_tweets": failed,
                    "output_dir": str(output_dir),
                },
            )
        except Exception as exc:  # pragma: no cover
            self._state.finish_error(str(exc))
            logger.exception(
                "Cache job failed.",
                extra={
                    "job_id": job_id,
                    "error": str(exc),
                },
            )
        finally:
            self._finish_worker_job(token)
