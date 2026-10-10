"""Active and queued Cache tasks remain visible independently of the selected source.

Code version: v1.1.0-claude.0
"""

from unittest.mock import Mock, patch

from flask import Flask

from app.core.cache_task_coordinator import CacheTaskCoordinator, CacheTaskIdentity
from app.core.state import TaskSnapshot, TaskState
from app.web.cache_routes import CacheRouteContext, CacheRuntimeAdapter, register_cache_routes


def activity_app(snapshots, coordinator=None):
    """Register only Cache routes with isolated in-memory task state per source and mode."""
    def state_for(snapshot):
        return TaskState("test", snapshot_factory=lambda _version: snapshot)

    dependencies = Mock()
    runtimes = {
        source: {
            mode: CacheRuntimeAdapter(
                state=state_for(snapshot),
                service=dependencies.service,
                hydrate_snapshot=dependencies.hydrate_snapshot,
            )
            for mode, snapshot in modes.items()
        }
        for source, modes in snapshots.items()
    }
    context = CacheRouteContext(
        cache_runtimes=runtimes,
        config_store=dependencies.config_store,
        media_catalog=dependencies.media_catalog,
        cache_task_coordinator=coordinator or Mock(task_states=Mock(return_value={})),
        reject_active_safari_agent_for_cache=dependencies.reject_active_safari_agent_for_cache,
        external_agent_operations_enabled=dependencies.external_agent_operations_enabled,
        reject_external_agent_operation=dependencies.reject_external_agent_operation,
    )
    app = Flask(__name__)
    register_cache_routes(app, context)
    return app, context, dependencies


def test_activity_lists_every_running_source_and_mode_with_its_registered_mode():
    application, _context, dependencies = activity_app(
        {
            "grok": {
                "media": TaskSnapshot(version="test", running=True, phase="collecting"),
                # The registered mode names the row; stale run metadata never does.
                "text": TaskSnapshot(
                    version="test", running=True, phase="stopping",
                    performance_metrics={"content_mode": "media"},
                ),
            },
            "chatgpt": {
                "text": TaskSnapshot(
                    version="test",
                    running=True,
                    phase="downloading",
                    processed_tweets=12,
                    queued_tweets=50,
                    progress_unit="sessions",
                ),
                "media": TaskSnapshot(version="test", running=True, phase="starting"),
            },
            "claude": {"text": TaskSnapshot(version="test", phase="finished")},
            "x": {"media": TaskSnapshot(version="test", phase="failed")},
        },
    )
    with patch(
        "app.web.cache_routes.build_reconciled_cache_snapshot",
        side_effect=AssertionError("Activity must not reconcile disk or browser state"),
    ):
        response = application.test_client().get("/api/cache/activity?content_mode=text")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    tasks = response.get_json()["tasks"]
    assert [task["id"] for task in tasks] == [
        "chatgpt:media", "chatgpt:text", "grok:media", "grok:text",
    ]
    assert [task["content_mode"] for task in tasks] == ["media", "text", "media", "text"]
    assert tasks[1] == {
        "id": "chatgpt:text",
        "source": "chatgpt",
        "label": "ChatGPT",
        "content_mode": "text",
        "phase": "downloading",
        "message": "Caching items.",
        "processed": 12,
        "total": 50,
        "unit": "sessions",
    }
    assert tasks[-1]["phase"] == "stopping"
    assert dependencies.mock_calls == []


def test_activity_omits_private_diagnostics_and_reads_only_valid_run_metadata():
    application, _context, dependencies = activity_app({
        "claude": {
            "media": TaskSnapshot(
                version="test",
                running=True,
                phase="custom-private-phase",
                message="Provider https://example.test/private-session failed",
                last_error="private diagnostic",
                output_dir="/private/cache/path",
                account_name="Private account",
                recent_events=["Private event"],
                performance_metrics={"content_mode": "text", "cookie": "secret"},
                progress_unit="private unit",
                processed_tweets=-1,
                queued_tweets=-2,
            ),
        },
    })
    task = application.test_client().get("/api/cache/activity").get_json()["tasks"][0]
    assert set(task) == {
        "id", "source", "label", "content_mode", "phase", "message", "processed", "total", "unit",
    }
    assert task["content_mode"] == "media"
    assert task["phase"] == "running"
    assert task["message"] == "Cache task in progress."
    assert task["processed"] == task["total"] == 0
    assert task["unit"] == "items"
    assert "private" not in str(task).lower()
    assert "secret" not in str(task)
    assert dependencies.mock_calls == []


def test_activity_removes_completed_tasks_without_remembering_previous_run():
    application, context, _dependencies = activity_app({
        "chatgpt": {"text": TaskSnapshot(version="test", running=True, phase="starting")},
    })
    client = application.test_client()
    assert len(client.get("/api/cache/activity").get_json()["tasks"]) == 1
    context.cache_runtimes["chatgpt"]["text"].state.finish_success("Done")
    assert client.get("/api/cache/activity").get_json() == {"tasks": []}


def test_activity_idle_runtime_has_no_active_tasks():
    application, _context, _dependencies = activity_app({
        "chatgpt": {
            "text": TaskSnapshot(version="test"),
            "media": TaskSnapshot(version="test"),
        },
    })
    assert application.test_client().get("/api/cache/activity").get_json() == {"tasks": []}


def test_activity_lists_queued_tasks_last_with_the_resource_they_wait_for(tmp_path):
    """A queued row names its blocker from registry labels, never from task output."""
    coordinator = CacheTaskCoordinator(tmp_path)

    def submit(source, mode, label, browser):
        coordinator.submit(
            CacheTaskIdentity(source, mode, label),
            launch=lambda _queued: None,
            on_queued=lambda _message: None,
            on_queue_update=lambda _message: None,
            on_launch_error=lambda _error: None,
            lock_busy_message="busy",
            browser=browser,
        )

    submit("x", "media", "X", "safari")
    submit("chatgpt", "text", "ChatGPT", "safari")
    application, _context, _dependencies = activity_app(
        {
            "chatgpt": {
                "text": TaskSnapshot(
                    version="test", running=True, phase="queued",
                    message="Private queue diagnostic https://example.test/private",
                ),
            },
            "x": {
                "media": TaskSnapshot(
                    version="test", running=True, phase="downloading",
                    processed_tweets=3, queued_tweets=9,
                ),
            },
        },
        coordinator,
    )
    try:
        tasks = application.test_client().get("/api/cache/activity").get_json()["tasks"]
    finally:
        coordinator.cancel("chatgpt:text")
        coordinator.finish("x:media")
    assert [(task["id"], task["phase"]) for task in tasks] == [
        ("x:media", "downloading"), ("chatgpt:text", "queued"),
    ]
    assert tasks[1]["message"] == (
        "Queued. Safari is busy with the X · Media cache. "
        "This task starts automatically when Safari is free."
    )
    assert tasks[1]["processed"] == tasks[1]["total"] == 0
    assert "private" not in str(tasks).lower()
