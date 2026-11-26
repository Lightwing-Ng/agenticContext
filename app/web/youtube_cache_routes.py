"""Run loopback-only YouTube cache jobs and serve their completed files."""

# Code version: v1.4.2-codex.0

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any
from uuid import uuid4

from flask import Blueprint, abort, current_app, jsonify, request, send_file, url_for

from app.core.agent import is_loopback_address
from app.core.foundation import CacheTaskBusyError, CacheTaskCoordinator, CacheTaskIdentity
from app.core.storage import (
    YouTubeCacheError,
    YouTubeCacheProgress,
    YouTubeCacheResult,
    download_youtube_media,
    parse_youtube_video_id,
    read_cached_youtube_media,
    recent_cached_youtube_media,
    youtube_cache_path,
)
from app.web.config_store import SavedConfigStore


def register_youtube_cache_routes(
    blueprint: Blueprint,
    root: Path,
    config_store: SavedConfigStore,
    coordinator: CacheTaskCoordinator,
    external_operations_enabled: Callable[[], bool],
    reject_external_operation: Callable[[], Any],
) -> Callable[[], bool]:
    """Expose a single download worker independent of browser-cache jobs."""
    jobs: dict[str, dict[str, Any]] = {}
    active: tuple[str, str, str] | None = None
    worker_thread: Thread | None = None
    lock = Lock()
    stop_event = Event()
    identity = CacheTaskIdentity(source="youtube", content_mode="media", label="YouTube")

    def shutdown() -> bool:
        """Cancel the owned subprocess and wait briefly for staging cleanup."""
        with lock:
            stop_event.set()
            worker = worker_thread
        if worker is None:
            return True
        worker.join(timeout=12)
        return not worker.is_alive()

    def attachment_payload(video_id: str, mode: str, result: YouTubeCacheResult) -> dict[str, str]:
        return {
            "filename": result.filename,
            "download_url": url_for(
                f"{blueprint.name}.youtube_cached_media", video_id=video_id, mode=mode,
            ),
        }

    def completed_payload(video_id: str, mode: str, result: YouTubeCacheResult) -> dict[str, Any]:
        return {
            **attachment_payload(video_id, mode, result),
            "status": "completed",
            "size_bytes": result.size_bytes,
            "cached": result.cached,
        }

    def start_response(job_id: str):
        return jsonify({
            "status_url": url_for(f"{blueprint.name}.youtube_download_status", job_id=job_id),
        }), 202

    def remember_job(job_id: str, job: dict[str, Any]) -> None:
        if len(jobs) >= 32:
            running_id = active[0] if active else None
            oldest = next(key for key in jobs if key != running_id)
            del jobs[oldest]
        jobs[job_id] = job

    def cached_response(video_id: str, mode: str, result: YouTubeCacheResult):
        job_id = uuid4().hex
        remember_job(job_id, completed_payload(video_id, mode, result))
        return start_response(job_id)

    @blueprint.post("/api/cache/youtube/download")
    def start_youtube_download():
        nonlocal active
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "YouTube caching is available on this computer only."}), 403
        if not external_operations_enabled():
            return reject_external_operation()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Enter a valid link to one YouTube video."}), 400
        mode = payload.get("mode")
        if mode not in ("video", "audio"):
            return jsonify({"error": "Choose Video or Audio."}), 400
        try:
            video_id = parse_youtube_video_id(payload.get("url"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        logger = current_app.logger
        max_size = config_store.config.max_media_file_size_bytes
        with lock:
            if stop_event.is_set():
                return jsonify({"error": "The YouTube cache is shutting down."}), 503
            if active is not None and active[1:] == (video_id, mode):
                return start_response(active[0])
            try:
                destination = youtube_cache_path(root, video_id, mode)
                existing = read_cached_youtube_media(destination)
            except (OSError, ValueError, YouTubeCacheError):
                return jsonify({"error": "The YouTube cache destination is unavailable."}), 409
            if existing is not None and not existing.needs_metadata_lookup:
                return cached_response(video_id, mode, existing)
            if active:
                if existing is not None:
                    return cached_response(video_id, mode, existing)
                return jsonify({"error": "A YouTube download is already running."}), 409
            job_id = uuid4().hex
            remember_job(job_id, {"status": "running"})
            active = (job_id, video_id, mode)

        def update_progress(progress: YouTubeCacheProgress | None) -> None:
            with lock:
                job: dict[str, Any] = {"status": "running"}
                if progress is not None:
                    job["progress"] = progress
                jobs[job_id] = job

        def run() -> None:
            nonlocal active, worker_thread
            try:
                result = download_youtube_media(
                    root, video_id, mode, max_size, should_stop=stop_event.is_set, on_progress=update_progress,
                )
                job = {"status": "completed", "result": result, "video_id": video_id, "mode": mode}
            except YouTubeCacheError as exc:
                job = {"status": "failed", "error": str(exc)}
            except Exception:
                logger.exception("YouTube cache worker failed.")
                job = {"status": "failed", "error": "The YouTube download failed. Please try again."}
            try:
                coordinator.finish(identity.key)
            finally:
                with lock:
                    jobs[job_id] = job
                    active = None
                    worker_thread = None

        def launch(_queued: bool) -> None:
            nonlocal worker_thread
            with lock:
                if stop_event.is_set():
                    raise RuntimeError("The YouTube cache is shutting down.")
                worker = Thread(name="youtube-cache", target=run, daemon=True)
                worker.start()
                worker_thread = worker

        try:
            coordinator.submit(
                identity,
                launch=launch,
                on_queued=lambda _message: None,
                on_queue_update=lambda _message: None,
                on_launch_error=lambda _error: None,
                lock_busy_message="A cache task or shadow backup is active in another window.",
                allow_queue=False,
            )
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, CacheTaskBusyError):
                if existing is not None:
                    with lock:
                        jobs[job_id] = completed_payload(video_id, mode, existing)
                        active = None
                    return start_response(job_id)
                message = str(exc)
                response_status = 409
            else:
                message = "The YouTube download could not start. Please try again."
                response_status = 503
            with lock:
                jobs[job_id] = {"status": "failed", "error": message}
                active = None
            return jsonify({"error": message}), response_status
        return start_response(job_id)

    @blueprint.get("/api/cache/youtube/jobs/<job_id>")
    def youtube_download_status(job_id: str):
        if not is_loopback_address(request.remote_addr):
            abort(403)
        with lock:
            job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "This YouTube download is no longer available."}), 404
        if "result" in job:
            job = completed_payload(job["video_id"], job["mode"], job["result"])
        response = jsonify(job)
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/api/cache/youtube/recent")
    def youtube_recent_media():
        if not is_loopback_address(request.remote_addr):
            abort(403)
        items = [
            attachment_payload(video_id, mode, media)
            for video_id, mode, media in recent_cached_youtube_media(root)
        ]
        response = jsonify({"items": items})
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/api/cache/youtube/media/<video_id>/<mode>")
    def youtube_cached_media(video_id: str, mode: str):
        if not is_loopback_address(request.remote_addr):
            abort(403)
        try:
            path = youtube_cache_path(root, video_id, mode)
            existing = read_cached_youtube_media(path)
        except (OSError, ValueError, YouTubeCacheError):
            abort(404)
        if existing is None:
            abort(404)
        return send_file(path, as_attachment=True, download_name=existing.filename)

    return shutdown
