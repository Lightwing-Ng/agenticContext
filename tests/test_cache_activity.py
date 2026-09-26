"""Active Cache tasks remain visible independently of the selected source.

Code version: v1.0.0-codex.0
"""

from unittest.mock import Mock, patch

from flask import Flask

from app.core.state import TaskSnapshot, TaskState
from app.web.cache_routes import CacheRouteContext, CacheRuntimeAdapter, register_cache_routes


def activity_app(snapshots, grok_text=None):
    """Register only Cache routes with isolated in-memory task state."""
    def state_for(snapshot):
        return TaskState("test", snapshot_factory=lambda _version: snapshot)

    dependencies = Mock()
    runtimes = {
        source: CacheRuntimeAdapter(
            state=state_for(snapshot),
            service=dependencies.service,
            hydrate_snapshot=dependencies.hydrate_snapshot,
        )
        for source, snapshot in snapshots.items()
    }
    context = CacheRouteContext(
        cache_runtimes=runtimes,
        config_store=dependencies.config_store,
        media_catalog=dependencies.media_catalog,
        grok_history_state=state_for(grok_text or TaskSnapshot(version="test")),
        grok_history_service=dependencies.grok_history_service,
        chatgpt_service=dependencies.chatgpt_service,
        reject_active_safari_agent_for_cache=dependencies.reject_active_safari_agent_for_cache,
        external_agent_operations_enabled=dependencies.external_agent_operations_enabled,
        reject_external_agent_operation=dependencies.reject_external_agent_operation,
    )
    app = Flask(__name__)
    register_cache_routes(app, context)
    return app, context, dependencies


def test_activity_lists_all_running_sources_and_both_grok_runtimes():
    application, _context, dependencies = activity_app(
        {
            "grok": TaskSnapshot(version="test", running=True, phase="collecting"),
            "chatgpt": TaskSnapshot(
                version="test",
                running=True,
                phase="downloading",
                processed_tweets=12,
                queued_tweets=50,
                progress_unit="sessions",
            ),
            "claude": TaskSnapshot(version="test", phase="finished"),
            "x": TaskSnapshot(version="test", phase="failed"),
        },
        grok_text=TaskSnapshot(
            version="test", running=True, phase="stopping", performance_metrics={"content_mode": "media"},
        ),
    )
    with patch(
        "app.web.cache_routes.build_reconciled_cache_snapshot",
        side_effect=AssertionError("Activity must not reconcile disk or browser state"),
    ):
        response = application.test_client().get("/api/cache/activity?content_mode=text")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    tasks = response.get_json()["tasks"]
    assert [task["id"] for task in tasks] == ["chatgpt", "grok:media", "grok:text"]
    assert [task["content_mode"] for task in tasks] == ["", "media", "text"]
    assert tasks[0] == {
        "id": "chatgpt",
        "source": "chatgpt",
        "label": "ChatGPT",
        "content_mode": "",
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
        "claude": TaskSnapshot(
            version="test",
            running=True,
            phase="custom-private-phase",
            message="Provider https://example.test/private-session failed",
            last_error="private diagnostic",
            output_dir="/private/cache/path",
            account_name="Private account",
            recent_events=["Private event"],
            performance_metrics={"content_mode": "media", "cookie": "secret"},
            progress_unit="private unit",
            processed_tweets=-1,
            queued_tweets=-2,
        ),
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
        "chatgpt": TaskSnapshot(version="test", running=True, phase="starting"),
    })
    client = application.test_client()
    assert len(client.get("/api/cache/activity").get_json()["tasks"]) == 1
    context.cache_runtimes["chatgpt"].state.finish_success("Done")
    assert client.get("/api/cache/activity").get_json() == {"tasks": []}


def test_activity_idle_runtime_has_no_active_tasks():
    application, _context, _dependencies = activity_app({
        "chatgpt": TaskSnapshot(version="test"),
    })
    assert application.test_client().get("/api/cache/activity").get_json() == {"tasks": []}
