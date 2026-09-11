"""Behavioral checks for the optional, isolated Beta navigation boundary.

Code version: v0.4.0-codex.1
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re

from flask import Flask, template_rendered
import pytest

from app.core.providers import (
    ZHIHU_EXAMPLE_PROFILE_URL,
    ZhihuArchiveError,
    ZhihuTaskBusyError,
)
from app.web.app import create_app
from app.web.beta import BETA_EXPERIMENTS


@pytest.fixture
def beta_app_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Flask]:
    """Keep every factory instance away from user settings and production stores."""
    monkeypatch.delenv("AGENTIC_CONTEXT_BETA_ENABLED", raising=False)
    created = 0

    def build(**options: object) -> Flask:
        nonlocal created
        created += 1
        sandbox = tmp_path / str(created)
        factory_options = dict(options)
        external_operations_enabled = bool(
            factory_options.pop("agent_external_operations_enabled", False)
        )
        application = create_app(
            sandbox / "local-store",
            computer_use_settings_path=sandbox / "settings" / "agent.json",
            computer_use_runtime_root=sandbox / "agent-runtime",
            agent_external_operations_enabled=external_operations_enabled,
            **factory_options,
        )
        application.config.update(TESTING=True)
        return application

    return build


def test_beta_index_and_each_experiment_render_only_the_selected_metadata(
    beta_app_factory: Callable[..., Flask],
) -> None:
    application = beta_app_factory()
    client = application.test_client()
    rendered: list[dict[str, object]] = []

    def record(sender: Flask, template: object, context: dict[str, object]) -> None:
        rendered.append(context)

    with template_rendered.connected_to(record, application):
        for route in ("/beta", "/beta/"):
            assert client.get(route).status_code == 200
            assert rendered[-1]["beta_experiment"] == BETA_EXPERIMENTS[0]
        for experiment in BETA_EXPERIMENTS:
            response = client.get(f"/beta/{experiment.id}")
            assert response.status_code == 200
            assert experiment.title in response.get_data(as_text=True)
            assert rendered[-1]["beta_experiment"] == experiment
            assert tuple(rendered[-1]["beta_experiments"]) == BETA_EXPERIMENTS
            assert rendered[-1]["beta_version"] == "v0.3.0"


def test_zhihu_cache_is_appended_without_changing_the_existing_beta_default() -> None:
    assert BETA_EXPERIMENTS[0].id == "idea-collision"
    assert [experiment.id for experiment in BETA_EXPERIMENTS[-2:]] == [
        "mission-forge",
        "zhihu-answers-cache",
    ]


def test_zhihu_cache_shares_the_injected_cache_task_lock(
    beta_app_factory: Callable[..., Flask],
) -> None:
    application = beta_app_factory()

    assert (
        application.extensions["beta_zhihu_answers_service"]._task_lock
        is application.extensions["shadow_backup_service"]._task_lock
    )


@pytest.mark.parametrize("route", ["/beta", "/beta/idea-collision"])
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_beta_has_no_mutating_http_routes(
    beta_app_factory: Callable[..., Flask],
    route: str,
    method: str,
) -> None:
    client = beta_app_factory().test_client()
    assert client.open(route, method=method, json={"prompt": "Do not execute"}).status_code == 405


def test_unknown_and_unplugged_experiments_return_not_found(
    beta_app_factory: Callable[..., Flask],
) -> None:
    application = beta_app_factory(beta_experiments=["mission-forge", "context-capsule"])
    client = application.test_client()
    body = client.get("/beta").get_data(as_text=True)

    assert client.get("/beta/context-capsule").status_code == 200
    assert client.get("/beta/mission-forge").status_code == 200
    assert client.get("/beta/idea-collision").status_code == 404
    assert client.get("/api/beta/zhihu-answers-cache/status").status_code == 404
    assert "beta_zhihu_answers_service" not in application.extensions
    assert client.get("/beta/not-installed").status_code == 404
    assert "/beta/idea-collision" not in body
    assert body.index("/beta/context-capsule") < body.index("/beta/mission-forge")


@pytest.mark.parametrize("options", [{"beta_enabled": False}, {"beta_experiments": []}])
def test_unplugging_beta_removes_its_routes_and_dock_without_changing_settings(
    beta_app_factory: Callable[..., Flask],
    options: dict[str, object],
) -> None:
    application = beta_app_factory(**options)
    client = application.test_client()
    response = client.get("/settings")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "beta" not in application.blueprints
    assert client.get("/beta").status_code == 404
    assert client.get("/beta/idea-collision").status_code == 404
    assert client.get("/api/beta/zhihu-answers-cache/status").status_code == 404
    assert 'data-dock-section="beta"' not in body
    assert re.findall(r'data-dock-section="([^"]+)"', body) == [
        "agent", "cache", "local-resources", "settings",
    ]
    assert 'style="--active-index: 4"' not in body


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "unrecognized"])
def test_environment_can_unplug_beta_with_explicit_factory_override(
    beta_app_factory: Callable[..., Flask],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("AGENTIC_CONTEXT_BETA_ENABLED", value)
    assert "beta" not in beta_app_factory().blueprints
    assert "beta" in beta_app_factory(beta_enabled=True).blueprints


@pytest.mark.parametrize("selected", [["not-installed"], "idea-collision"])
def test_invalid_plugin_selection_fails_before_service_initialization(
    beta_app_factory: Callable[..., Flask],
    monkeypatch: pytest.MonkeyPatch,
    selected: object,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Invalid Beta configuration reached production service initialization")

    monkeypatch.setattr("app.web.app.LocalMediaCatalog", forbidden)
    with pytest.raises(ValueError, match="Beta experiment IDs|beta_experiments"):
        beta_app_factory(beta_experiments=selected)


def test_beta_dock_precedes_settings_and_avoids_existing_destination_memory(
    beta_app_factory: Callable[..., Flask],
) -> None:
    client = beta_app_factory().test_client()
    settings = client.get("/settings").get_data(as_text=True)
    beta = client.get("/beta").get_data(as_text=True)

    assert re.findall(r'data-dock-section="([^"]+)"', settings) == [
        "agent", "cache", "local-resources", "beta", "settings",
    ]
    assert 'style="--active-index: 4"' in settings
    dock_link = re.search(r'<a\b[^>]*data-dock-section="beta"[^>]*>', beta)
    assert dock_link is not None
    assert 'href="/beta"' in dock_link.group(0)
    assert 'aria-current="page"' in dock_link.group(0)
    assert 'data-section-link=' not in dock_link.group(0)


@pytest.mark.parametrize("route", ["/cache/x", "/browser", "/settings", "/agent"])
def test_existing_pages_do_not_load_beta_runtime_or_styles(
    beta_app_factory: Callable[..., Flask],
    route: str,
) -> None:
    response = beta_app_factory().test_client().get(route, follow_redirects=True)
    assert response.status_code == 200
    assert "/static/beta" not in response.get_data(as_text=True)


def test_beta_requests_do_not_access_production_content_or_services(
    beta_app_factory: Callable[..., Flask],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    application = beta_app_factory()
    store = application.extensions["local_media_catalog"].local_store_root
    store.mkdir(parents=True, exist_ok=True)
    sentinel = store / "private-resource.txt"
    sentinel.write_text("Private cached content must stay outside Beta", encoding="utf-8")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Beta accessed a production content or service boundary")

    for name in (
        "query_chat_history", "save_config", "probe_browser_session",
        "build_initial_snapshot", "build_chatgpt_initial_snapshot",
        "build_grok_initial_snapshot", "list_agent_sources",
    ):
        monkeypatch.setattr(f"app.web.app.{name}", forbidden)
    for extension, methods in (
        ("prompt_store", ("query", "add_pointer", "add_remark")),
        ("agent_source_cache", ("get_or_collect", "store")),
        ("computer_use_agent_service", ("start", "request_stop")),
        ("local_media_catalog", ("delete", "restore")),
    ):
        for method in methods:
            monkeypatch.setattr(application.extensions[extension], method, forbidden)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    client = application.test_client()
    for experiment in BETA_EXPERIMENTS:
        response = client.get(f"/beta/{experiment.id}")
        assert response.status_code == 200
        assert "Private cached content must stay outside Beta" not in response.get_data(as_text=True)

    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_zhihu_cache_page_exposes_only_chromium_browser_choices(
    beta_app_factory: Callable[..., Flask],
) -> None:
    application = beta_app_factory()
    response = application.test_client().get("/beta/zhihu-answers-cache")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'value="edge"' in body
    assert 'value="chrome"' in body
    assert 'value="safari"' not in body
    assert "333" not in body


def test_zhihu_cache_api_has_explicit_status_start_and_stop_contract(
    beta_app_factory: Callable[..., Flask],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = beta_app_factory(agent_external_operations_enabled=True)
    service = application.extensions["beta_zhihu_answers_service"]
    calls: list[tuple[str, str]] = []
    status = {
        "running": False,
        "phase": "idle",
        "profile_url": "https://www.zhihu.com/people/feifeimao/answers",
        "author_token": "feifeimao",
        "browser": "edge",
        "cached_answers": 0,
        "recent_answers": [],
    }

    def fake_snapshot(profile_url: str | None = None) -> dict[str, object]:
        if profile_url:
            calls.append(("status", profile_url))
        return dict(status)

    def fake_start(config: object, profile_url: str, browser_id: str) -> None:
        calls.append((profile_url, browser_id))

    monkeypatch.setattr(service, "snapshot", fake_snapshot)
    monkeypatch.setattr(service, "start", fake_start)
    monkeypatch.setattr(service, "request_stop", lambda: True)
    client = application.test_client()

    get_response = client.get(
        "/api/beta/zhihu-answers-cache/status",
        query_string={"profile_url": "https://www.zhihu.com/people/feifeimao/answers?page=2"},
    )
    start_response = client.post(
        "/api/beta/zhihu-answers-cache/start",
        json={
            "profile_url": "https://www.zhihu.com/people/feifeimao/answers?page=2",
            "browser": "edge",
        },
    )
    stop_response = client.post("/api/beta/zhihu-answers-cache/stop")

    assert get_response.status_code == 200
    assert start_response.status_code == 202
    assert start_response.get_json() == {"status": status}
    assert stop_response.status_code == 202
    assert stop_response.get_json() == {"status": status, "stop_requested": True}
    assert calls == [
        ("status", "https://www.zhihu.com/people/feifeimao/answers?page=2"),
        ("https://www.zhihu.com/people/feifeimao/answers?page=2", "edge"),
    ]


def test_zhihu_cache_api_browses_and_reads_only_the_requested_local_answer(
    beta_app_factory: Callable[..., Flask],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = beta_app_factory()
    service = application.extensions["beta_zhihu_answers_service"]
    calls: list[tuple[object, ...]] = []

    def fake_browse(
        profile_url: str,
        *,
        query: str,
        page: int,
        page_size: int,
    ) -> dict[str, object]:
        calls.append(("browse", profile_url, query, page, page_size))
        return {
            "profile_url": "https://www.zhihu.com/people/feifeimao/answers",
            "author_token": "feifeimao",
            "query": query,
            "page": page,
            "page_size": page_size,
            "page_count": 1,
            "total": 1,
            "items": [{"answer_id": "2197549311"}],
        }

    def fake_read(profile_url: str, answer_id: str) -> dict[str, object]:
        calls.append(("read", profile_url, answer_id))
        return {
            "answer_id": answer_id,
            "question_title": "Fixture title",
            "content_text": "Cached fixture body",
            "content_sha256": "a" * 64,
        }

    monkeypatch.setattr(service, "browse_answers", fake_browse)
    monkeypatch.setattr(service, "read_answer", fake_read)
    client = application.test_client()
    profile_url = "https://www.zhihu.com/people/feifeimao/answers?page=2"

    browse_response = client.get(
        "/api/beta/zhihu-answers-cache/answers",
        query_string={"profile_url": profile_url, "q": "2197549311", "page": 2, "page_size": 10},
    )
    answer_response = client.get(
        "/api/beta/zhihu-answers-cache/answers/2197549311",
        query_string={"profile_url": profile_url},
    )

    assert browse_response.status_code == 200
    assert browse_response.get_json()["items"] == [{"answer_id": "2197549311"}]
    assert answer_response.status_code == 200
    assert answer_response.get_json()["content_text"] == "Cached fixture body"
    assert browse_response.headers["Cache-Control"] == "no-store"
    assert answer_response.headers["Cache-Control"] == "no-store"
    assert calls == [
        ("browse", profile_url, "2197549311", 2, 10),
        ("read", profile_url, "2197549311"),
    ]


def test_zhihu_cache_read_api_bounds_queries_and_hides_archive_failures(
    beta_app_factory: Callable[..., Flask],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = beta_app_factory()
    service = application.extensions["beta_zhihu_answers_service"]
    client = application.test_client()

    invalid_page = client.get(
        "/api/beta/zhihu-answers-cache/answers",
        query_string={"page": "not-a-number"},
    )
    missing = client.get("/api/beta/zhihu-answers-cache/answers/2197549311")
    monkeypatch.setattr(
        service,
        "read_answer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private fixture path must not escape")
        ),
    )
    failed = client.get("/api/beta/zhihu-answers-cache/answers/2197549311")

    assert invalid_page.status_code == 400
    assert invalid_page.get_json()["code"] == "invalid_zhihu_archive_query"
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "zhihu_answer_not_found"
    assert failed.status_code == 500
    assert failed.get_json()["code"] == "zhihu_archive_unavailable"
    assert "private fixture" not in failed.get_data(as_text=True)


@pytest.mark.parametrize(
    ("route", "method"),
    [
        ("/api/beta/zhihu-answers-cache/status", "POST"),
        ("/api/beta/zhihu-answers-cache/answers", "POST"),
        ("/api/beta/zhihu-answers-cache/answers/1", "POST"),
        ("/api/beta/zhihu-answers-cache/start", "GET"),
        ("/api/beta/zhihu-answers-cache/stop", "GET"),
    ],
)
def test_zhihu_cache_api_rejects_unsupported_methods(
    beta_app_factory: Callable[..., Flask],
    route: str,
    method: str,
) -> None:
    assert beta_app_factory().test_client().open(route, method=method).status_code == 405


def test_zhihu_cache_api_rejects_invalid_json_urls_and_browsers_without_starting(
    beta_app_factory: Callable[..., Flask],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = beta_app_factory(agent_external_operations_enabled=True)
    service = application.extensions["beta_zhihu_answers_service"]
    client = application.test_client()

    assert client.post(
        "/api/beta/zhihu-answers-cache/start",
        data="not-json",
        content_type="text/plain",
    ).status_code == 400
    assert client.get(
        "/api/beta/zhihu-answers-cache/status",
        query_string={"profile_url": "https://example.com/people/feifeimao/answers"},
    ).status_code == 400

    monkeypatch.setattr(
        service,
        "start",
        lambda config, profile_url, browser_id: (_ for _ in ()).throw(
            ValueError("Zhihu answer caching requires Chrome or Edge.")
        ),
    )
    response = client.post(
        "/api/beta/zhihu-answers-cache/start",
        json={
            "profile_url": "https://www.zhihu.com/people/feifeimao/answers",
            "browser": "safari",
        },
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_zhihu_request"


def test_zhihu_start_is_disabled_in_an_isolated_application(
    beta_app_factory: Callable[..., Flask],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = beta_app_factory(agent_external_operations_enabled=False)
    service = application.extensions["beta_zhihu_answers_service"]
    monkeypatch.setattr(
        service,
        "start",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Disabled app reached host browser authority")
        ),
    )

    response = application.test_client().post(
        "/api/beta/zhihu-answers-cache/start",
        json={"profile_url": ZHIHU_EXAMPLE_PROFILE_URL, "browser": "edge"},
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "external_operations_disabled"


def test_zhihu_start_returns_bounded_json_for_local_storage_failure(
    beta_app_factory: Callable[..., Flask],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = beta_app_factory(agent_external_operations_enabled=True)
    service = application.extensions["beta_zhihu_answers_service"]
    monkeypatch.setattr(
        service,
        "start",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("private fixture path must not escape")
        ),
    )

    response = application.test_client().post(
        "/api/beta/zhihu-answers-cache/start",
        json={"profile_url": ZHIHU_EXAMPLE_PROFILE_URL, "browser": "edge"},
    )

    assert response.status_code == 500
    assert response.is_json
    assert response.get_json() == {
        "code": "zhihu_storage_error",
        "error": "Zhihu Answers Cache could not access its local storage.",
    }
    assert "private fixture path" not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("start_error", "expected_status", "expected_code", "expected_message"),
    (
        (
            ZhihuTaskBusyError("A cache task is already running."),
            409,
            "cache_task_busy",
            "A cache task is already running.",
        ),
        (
            ZhihuArchiveError("private fixture path must not escape"),
            409,
            "zhihu_storage_boundary",
            "Zhihu Answers Cache rejected an unsafe storage boundary.",
        ),
        (
            RuntimeError("private fixture thread failure"),
            500,
            "zhihu_start_failed",
            "Zhihu Answers Cache could not start its background worker.",
        ),
    ),
)
def test_zhihu_start_classifies_busy_boundary_and_worker_failures(
    beta_app_factory: Callable[..., Flask],
    monkeypatch: pytest.MonkeyPatch,
    start_error: RuntimeError,
    expected_status: int,
    expected_code: str,
    expected_message: str,
) -> None:
    application = beta_app_factory(agent_external_operations_enabled=True)
    service = application.extensions["beta_zhihu_answers_service"]

    def fail_start(*_args: object, **_kwargs: object) -> None:
        raise start_error

    monkeypatch.setattr(service, "start", fail_start)
    response = application.test_client().post(
        "/api/beta/zhihu-answers-cache/start",
        json={"profile_url": ZHIHU_EXAMPLE_PROFILE_URL, "browser": "edge"},
    )

    assert response.status_code == expected_status
    assert response.get_json() == {
        "code": expected_code,
        "error": expected_message,
    }
    assert "private fixture" not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("remote_addr", "host", "origin", "expected_status"),
    [
        ("203.0.113.10", "localhost:8666", "", 403),
        ("127.0.0.1", "attacker.example", "", 403),
        ("192.168.1.25", "192.168.1.10:8666", "", 401),
        ("127.0.0.1", "localhost:8666", "http://attacker.example", 403),
    ],
)
def test_zhihu_api_rejects_untrusted_network_origin_or_locked_lan_session(
    beta_app_factory: Callable[..., Flask],
    remote_addr: str,
    host: str,
    origin: str,
    expected_status: int,
) -> None:
    headers = {"Host": host}
    if origin:
        headers["Origin"] = origin
    response = beta_app_factory().test_client().get(
        "/api/beta/zhihu-answers-cache/status",
        headers=headers,
        environ_overrides={"REMOTE_ADDR": remote_addr},
    )

    assert response.status_code == expected_status
    assert response.is_json
    assert response.get_json()["code"] in {"agent_access_required", "forbidden_origin"}
    assert response.headers["Cache-Control"] == "no-store"
    assert b"beta_store" not in response.data
