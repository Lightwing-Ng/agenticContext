"""Behavioral checks for the optional, isolated Beta navigation boundary.

Code version: v0.1.0
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re

from flask import Flask, template_rendered
import pytest

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
        application = create_app(
            sandbox / "local-store",
            computer_use_settings_path=sandbox / "settings" / "agent.json",
            computer_use_runtime_root=sandbox / "agent-runtime",
            agent_external_operations_enabled=False,
            **options,
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
            assert rendered[-1]["beta_version"] == "v0.1.0"


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
