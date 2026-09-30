"""Route-level coverage for Cache tasks that run together or wait in the queue.

Code version: v1.0.0-claude.0
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.core.config import CrawlConfig
from app.web.app import create_app


SERVICE_MODULES = (
    "app.core.service",
    "app.core.chatgpt_service",
    "app.core.grok_service",
    "app.core.grok_history_service",
    "app.core.gemini_service",
    "app.core.claude_history_service",
    "app.core.zhihu_history_service",
)


class _IdleThread:
    """Admit a worker without running it, so the test decides when it finishes."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass


@pytest.fixture
def cache_app(tmp_path: Path, macos_host) -> Iterator[Flask]:
    """Build an app whose cache workers are admitted but never run a browser."""
    config = CrawlConfig(
        x_browser="safari",
        chatgpt_browser="safari",
        grok_browser="safari",
        gemini_browser="safari",
        claude_browser="safari",
        zhihu_browser="edge",
    )
    with ExitStack() as stack:
        stack.enter_context(patch("app.web.config_store.load_saved_config", return_value=config))
        stack.enter_context(patch("app.web.config_store.save_config"))
        for module in SERVICE_MODULES:
            stack.enter_context(patch(f"{module}.Thread", _IdleThread))
        application = create_app(tmp_path / "local_store")
        application.config.update(TESTING=True)
        try:
            yield application
        finally:
            # The lock files are shared by every app in this process; leave none held.
            coordinator = application.extensions["cache_task_coordinator"]
            for key, state in list(coordinator.task_states().items()):
                if state["status"] == "queued":
                    coordinator.cancel(key)
            for key in list(coordinator.task_states()):
                coordinator.finish(key)


def _start(client: FlaskClient, source: str, mode: str, browser: str) -> None:
    response = client.post(
        f"/cache/{source}/start",
        data={"cache_content_mode": mode, f"{source}_browser": browser},
    )
    assert response.status_code == 302
    assert response.location == f"/cache/{source}/{mode}/{browser}"


def _status(client: FlaskClient, source: str, mode: str) -> dict:
    return client.get(f"/api/cache/{source}/status?content_mode={mode}").get_json()


def _activity(client: FlaskClient) -> list[tuple[str, str]]:
    tasks = client.get("/api/cache/activity").get_json()["tasks"]
    return [(task["id"], task["phase"]) for task in tasks]


def _finish(application: Flask, source: str, mode: str) -> None:
    runtime = application.extensions["cache_runtimes"][source][mode]
    runtime.state.finish_success("Finished.")
    runtime.service._release_task_lock()


def test_second_safari_task_queues_and_starts_when_the_first_finishes(cache_app: Flask) -> None:
    client = cache_app.test_client()

    _start(client, "x", "media", "safari")
    _start(client, "chatgpt", "text", "safari")

    assert _status(client, "x", "media")["phase"] == "starting"
    queued = _status(client, "chatgpt", "text")
    assert queued["running"] is True
    assert queued["phase"] == "queued"
    assert queued["message"] == (
        "Queued. Safari is busy with the X · Media cache. "
        "This task starts automatically when Safari is free."
    )
    # Nothing was written yet, so the page keeps the store's totals while it waits.
    idle_totals = cache_app.extensions["cache_runtimes"]["chatgpt"]["text"].hydrate_snapshot()
    assert queued["cached_messages"] == idle_totals.downloaded_tweets
    assert queued["output_dir"] == idle_totals.output_dir
    assert queued["started_at"]
    # The same source's other mode keeps its own state and remains startable.
    assert _status(client, "chatgpt", "media")["running"] is False
    assert _status(client, "x", "text")["running"] is False
    assert _activity(client) == [("x:media", "starting"), ("chatgpt:text", "queued")]
    # The entry on every page gives the same reason the task's own page does.
    queued_row = client.get("/api/cache/activity").get_json()["tasks"][1]
    assert queued_row["message"] == queued["message"]
    page = client.get("/cache/chatgpt/text/safari").get_data(as_text=True)
    assert 'data-phase="queued"' in page
    assert 'class="status-progress is-unavailable"' in page
    assert 'id="stop_button" >Stop</button>' in page

    _finish(cache_app, "x", "media")

    started = _status(client, "chatgpt", "text")
    assert started["running"] is True
    assert started["phase"] == "starting"
    assert started["message"] == "Initializing job."
    assert _activity(client) == [("chatgpt:text", "starting")]


def test_tasks_on_different_browsers_and_stores_run_together(cache_app: Flask) -> None:
    client = cache_app.test_client()

    _start(client, "x", "media", "chrome")
    _start(client, "chatgpt", "text", "safari")
    _start(client, "zhihu", "text", "edge")
    # Chromium X media does not record liked text, so X text runs beside it.
    _start(client, "x", "text", "chrome")
    # The other ChatGPT mode is an independent task; it only waits for Safari.
    _start(client, "chatgpt", "media", "safari")

    assert _activity(client) == [
        ("chatgpt:text", "starting"),
        ("x:media", "starting"),
        ("x:text", "starting"),
        ("zhihu:text", "starting"),
        ("chatgpt:media", "queued"),
    ]
    states = cache_app.extensions["cache_task_coordinator"].task_states()
    assert states["chatgpt:media"]["blocked_by"] == "ChatGPT · Text"
    assert states["x:media"]["browser"] == "chrome"


def test_safari_x_media_and_x_text_share_the_liked_text_store(cache_app: Flask) -> None:
    client = cache_app.test_client()

    _start(client, "x", "media", "safari")
    _start(client, "x", "text", "chrome")

    queued = _status(client, "x", "text")
    assert queued["phase"] == "queued"
    assert queued["message"] == (
        "Queued. This task starts automatically after the X · Media cache finishes."
    )


def test_stopping_a_queued_task_removes_it_and_leaves_the_running_task_alone(cache_app: Flask) -> None:
    client = cache_app.test_client()
    _start(client, "x", "media", "safari")
    _start(client, "chatgpt", "text", "safari")

    response = client.post("/cache/chatgpt/stop", data={"cache_content_mode": "text"})

    assert response.status_code == 302
    stopped = _status(client, "chatgpt", "text")
    assert stopped["running"] is False
    assert stopped["phase"] == "stopped"
    assert stopped["message"] == "The ChatGPT · Text cache left the queue before it started."
    assert _activity(client) == [("x:media", "starting")]
    # The mode a stop form names is the only one it stops.
    client.post("/cache/x/stop", data={"cache_content_mode": "text"})
    assert _status(client, "x", "media")["phase"] == "starting"
    client.post("/cache/x/stop", data={"cache_content_mode": "media"})
    assert _status(client, "x", "media")["phase"] == "stopping"


def test_legacy_stop_without_a_mode_stops_every_mode_of_the_source(cache_app: Flask) -> None:
    client = cache_app.test_client()
    _start(client, "x", "media", "chrome")
    _start(client, "x", "text", "chrome")

    assert client.post("/stop").status_code == 302

    assert _status(client, "x", "media")["phase"] == "stopping"
    assert _status(client, "x", "text")["phase"] == "stopping"


def test_safari_cache_blocks_a_safari_agent_by_the_browser_the_task_started_with(cache_app: Flask) -> None:
    client = cache_app.test_client()
    pool = cache_app.extensions["agent_session_pool"]
    ask = {
        "prompt": "Do not submit",
        "workspace_path": "/tmp",
        "operating_system": "macos",
        "browser": "safari",
        "platform": "grok",
        "model": "grok-build",
    }

    def ask_agent():
        with patch.object(pool, "start", side_effect=RuntimeError("admitted")) as start:
            response = client.post("/api/agent/ask", json=ask)
        return response, start

    # A cache task in another browser leaves Safari to the Agent.
    _start(client, "zhihu", "text", "edge")
    response, start = ask_agent()
    assert response.get_json().get("code") != "safari_cache_busy"
    start.assert_called_once()

    _start(client, "x", "media", "safari")
    # Starting the other mode in Chrome rewrites the saved X browser, but the media
    # task that is already running still owns Safari.
    _start(client, "x", "text", "chrome")
    response, start = ask_agent()
    assert response.status_code == 409
    assert response.get_json()["code"] == "safari_cache_busy"
    assert "X · Media" in response.get_json()["error"]
    start.assert_not_called()


def test_targeted_refresh_fails_at_once_instead_of_queueing(cache_app: Flask) -> None:
    client = cache_app.test_client()
    _start(client, "chatgpt", "text", "safari")

    response = client.post(
        "/api/browser/chatgpt/session/refresh",
        json={"conversation_url": "https://chatgpt.com/c/demo-session"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == (
        "Safari is busy with the ChatGPT · Text cache. "
        "Start the ChatGPT · Media cache after it finishes."
    )
    media = _status(client, "chatgpt", "media")
    assert media["running"] is False
    assert media["phase"] == "idle"
    assert _activity(client) == [("chatgpt:text", "starting")]

    _finish(cache_app, "chatgpt", "text")
    accepted = client.post(
        "/api/browser/chatgpt/session/refresh",
        json={"conversation_url": "https://chatgpt.com/c/demo-session"},
    )
    assert accepted.status_code == 202
    assert _activity(client) == [("chatgpt:media", "starting")]


def test_safari_account_probe_yields_to_a_running_safari_cache_and_offers_the_queue(cache_app: Flask) -> None:
    client = cache_app.test_client()
    probe_payload = {"browser": "edge", "logged_in": True, "can_download": True, "account_name": "A"}

    with patch("app.web.agent_routes.probe_browser_session", return_value=probe_payload) as probe:
        before = client.get("/api/browser-session?platform=chatgpt&browser=safari")
        _start(client, "x", "media", "safari")
        busy = client.get("/api/browser-session?platform=chatgpt&browser=safari")
        other_browser = client.get("/api/browser-session?platform=zhihu&browser=edge")

    assert before.get_json() == probe_payload
    assert busy.status_code == 200
    payload = busy.get_json()
    assert payload["busy"] is True
    assert payload["queue_available"] is True
    assert payload["can_download"] is False
    assert "logged_in" not in payload
    assert payload["message"] == (
        "Safari is busy with the X · Media cache. "
        "Start adds this task to the queue; the account is checked when it runs."
    )
    assert other_browser.get_json() == probe_payload
    # Safari was probed once, before the cache task owned it.
    assert [call.args[:2] for call in probe.call_args_list] == [("chatgpt", "safari"), ("zhihu", "edge")]


def test_chatgpt_reset_waits_for_either_mode(cache_app: Flask) -> None:
    client = cache_app.test_client()
    _start(client, "chatgpt", "text", "safari")

    with patch("app.web.cache_routes.reset_chatgpt_state") as reset:
        response = client.post("/chatgpt/reset")

    assert response.status_code == 302
    reset.assert_not_called()
    media_state = cache_app.extensions["cache_runtimes"]["chatgpt"]["media"].state.snapshot()
    assert media_state["last_error"] == "Cannot reset ChatGPT state while a sync is running."
