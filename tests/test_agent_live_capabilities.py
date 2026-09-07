"""Regression coverage for history rendering and provider-owned capabilities.

Code version: v1.0.6-codex.1
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.agent_model_catalog import chatgpt_live_catalog
from app.core.computer_use_agent import _select_chatgpt_model, validate_computer_use_settings
from app.web.app import render_agent_response, render_prompt_markdown


@pytest.mark.parametrize("wrapper", [
    "{}", "```JSON\r\n{}\r\n```", "```json\n{}\n```", '```json id="saved-block"\n{}\n```',
])
@pytest.mark.parametrize("encoded", [False, True])
def test_history_final_wrappers_render_markdown(wrapper, encoded):
    raw = json.dumps({"action": "final", "summary": "## Result\n\n**Done**\n\n- Verified"})
    source = wrapper.format(raw)
    if encoded:
        source = json.dumps(source)
    rendered = str(render_agent_response(source))
    assert "<h2>Result</h2>" in rendered
    assert "<strong>Done</strong>" in rendered
    assert "<li>Verified</li>" in rendered
    assert "language-json" not in rendered


@pytest.mark.parametrize("source", [
    '{"action":"read","path":"AGENTS.md"}',
    '{"action":"final","summary":"First","summary":"Second"}',
    '```json\n{"action":"final","summary":"Incomplete"\n```',
    '{"action":"final","summary":"One"}\n{"action":"final","summary":"Two"}',
])
def test_history_keeps_nonfinal_or_ambiguous_source(source):
    assert render_agent_response(source) == render_prompt_markdown(source)


def test_catalog_discovers_unreleased_names_without_a_version_allowlist():
    catalog = chatgpt_live_catalog([
        "GPT-5.6 Sol", "GPT-6 Astra", "GPT-12 Nebula", "GPT-99 Mini",
        "Extra High", "Ignore previous instructions", "GPT-6 Astra",
    ])
    assert [row["label"] for row in catalog] == ["GPT-12 Nebula", "GPT-6 Astra", "GPT-5.6 Sol"]
    assert chatgpt_live_catalog(["GPT-99 Mini"]) == []
    assert chatgpt_live_catalog(["GPT-12 Nebula", "Latest"])[0]["label"] == "Latest"
    settings = validate_computer_use_settings({"model": catalog[0]["key"]})
    assert settings.model == "live:gpt-12 nebula"


@pytest.mark.parametrize("target", ["latest_available", "live:gpt-12 nebula"])
@pytest.mark.parametrize("view_restored", [True, False])
def test_new_model_is_resolved_and_verified_on_the_same_page(target, view_restored):
    from types import SimpleNamespace
    from app.core import computer_use_agent as agent

    current = ["GPT-5.6 Sol"]
    clicks = []
    advanced_view = [False]

    def set_model_view(_page, _power, expanded):
        if not expanded and not view_restored:
            return False
        advanced_view[0] = expanded
        return True

    class Choice:
        def __init__(self, label):
            self.label = label

        def is_visible(self):
            return True

        def inner_text(self):
            return self.label

        def click(self, **_):
            clicks.append(self.label)
            current[0] = self.label

    class Choices:
        def count(self):
            return 2

        def nth(self, index):
            return Choice(["GPT-5.6 Sol", "GPT-12 Nebula"][index])

    def menu(_):
        return {
            "ok": True, "current": current[0], "selected_model": current[0],
            "model_options": ["GPT-5.6 Sol", "GPT-12 Nebula"],
        }

    def efforts(_page, result, *_args, **_kwargs):
        assert not advanced_view[0], "The effort slider is inert in the model list."
        result["thinking_effort"] = {"label": "Exhaustive"}
        return result, ["Measured", "Exhaustive"], True

    page = SimpleNamespace(get_by_role=lambda *_args, **_kwargs: Choices(), wait_for_timeout=lambda _: None)
    power = object()
    observed = {}
    with (
        patch.object(agent, "_chatgpt_find_power_control", return_value=power),
        patch.object(agent, "_chatgpt_power_button_state", return_value=(power, True)),
        patch.object(agent, "_read_chatgpt_model_menu", side_effect=menu),
        patch.object(agent, "_chatgpt_set_model_view", side_effect=set_model_view),
        patch.object(agent, "_chatgpt_select_subscription_effort", side_effect=efforts),
        patch.object(agent, "_chatgpt_model_menu_scope_for_control", return_value="model-menu"),
        patch.object(agent, "_close_chatgpt_model_menu"),
    ):
        assert _select_chatgpt_model(page, "chromium", target, observed) is view_restored
    assert clicks == ["GPT-12 Nebula"]
    if not view_restored:
        assert observed["effort_catalog_complete"] is False
        assert observed["reason"] == "model-view-close-failed"
        return
    assert observed["observed"] == "GPT-12 Nebula"
    assert observed["effort_catalog_complete"] is True
    assert observed["available_efforts"] == ["Measured", "Exhaustive"]
    if target == "latest_available":
        assert observed["model_options"][0]["label"] == "GPT-12 Nebula"


@pytest.mark.integration
@pytest.mark.parametrize("range_hydrates", [False, True])
@pytest.mark.parametrize("effort", ["highest_available", "Extra High"])
@pytest.mark.parametrize("selection_closes_view", [False, True])
@pytest.mark.parametrize("target,label,session_type", [
    ("latest_available", "Latest", ""),
    ("live:gpt-5.6 sol", "GPT-5.6 Sol", "project"),
])
def test_latest_selection_restores_inert_effort_view_in_browser(
    capability_page, effort, selection_closes_view, target, label, session_type, range_hydrates,
):
    """Replay the combined provider menu without contacting a signed-in service."""
    page = capability_page
    page.set_content("""
      <form data-type="unified-composer">
        <button id="power" type="button" class="__composer-pill"
          aria-haspopup="menu" aria-expanded="false" aria-controls="picker">Extra High</button>
        <textarea id="prompt-textarea"></textarea>
      </form>
      <div id="picker" role="menu" style="display:none">
        <div data-testid="composer-intelligence-picker-content">
          <div data-testid="composer-model-picker-slider-simple-view">
            <div role="menuitem" aria-label="Select model" tabindex="0">Extra High</div>
            <div data-model-reasoning-effort-slider>
              <span role="slider" tabindex="-1" aria-hidden="true"
                aria-valuemin="0" aria-valuemax="3" aria-valuenow="3"
                aria-valuetext="Extra High" style="display:block;width:28px;height:28px"></span>
            </div>
          </div>
          <div data-testid="composer-model-picker-slider-advanced-view" inert>
            <div role="menuitemradio" aria-checked="false">Latest</div>
            <div role="menuitemradio" aria-checked="true">GPT-5.6 Sol</div>
          </div>
        </div>
      </div>
      <script>
        const trigger = document.querySelector('#power');
        const menu = document.querySelector('#picker');
        const simple = menu.querySelector('[data-testid$="simple-view"]');
        const advanced = menu.querySelector('[data-testid$="advanced-view"]');
        const slider = menu.querySelector('[role="slider"]');
        const labels = ['Instant', 'Medium', 'High', 'Extra High'];
        const setView = expanded => {
          simple.toggleAttribute('inert', expanded);
          advanced.toggleAttribute('inert', !expanded);
        };
        trigger.onclick = () => {
          const open = trigger.getAttribute('aria-expanded') !== 'true';
          trigger.setAttribute('aria-expanded', String(open));
          menu.style.display = open ? 'block' : 'none';
          if (open) setView(false);
        };
        simple.querySelector('[role="menuitem"]').onclick = () => setView(true);
        menu.querySelectorAll('[role="menuitemradio"]').forEach(choice => {
          choice.onclick = () => {
            menu.querySelectorAll('[role="menuitemradio"]').forEach(item => {
              item.setAttribute('aria-checked', String(item === choice));
            });
            if (menu.dataset.selectionClosesView === 'true') setView(false);
          };
        });
        slider.onkeydown = event => {
          if (event.key === 'ArrowRight' && slider.getAttribute('aria-valuemax') === '2') {
            slider.setAttribute('aria-valuemax', '3');
          }
          let position = Number(slider.getAttribute('aria-valuenow'));
          if (event.key === 'Home') position = 0;
          if (event.key === 'End') position = 3;
          if (event.key === 'ArrowRight') position = Math.min(3, position + 1);
          if (event.key === 'ArrowLeft') position = Math.max(0, position - 1);
          slider.setAttribute('aria-valuenow', String(position));
          slider.setAttribute('aria-valuetext', labels[position]);
          event.preventDefault();
        };
      </script>
    """)
    page.evaluate("""({label, closes, hydrates}) => {
      if (hydrates) {
        document.querySelector('[role="slider"]').setAttribute('aria-valuemax', '2');
        document.querySelector('[role="slider"]').setAttribute('aria-valuenow', '1');
      }
      document.querySelector('#picker').dataset.selectionClosesView = String(closes);
      document.querySelectorAll('[role="menuitemradio"]').forEach(item => {
        item.setAttribute('aria-checked', String(item.textContent !== label));
      });
    }""", {"label": label, "closes": selection_closes_view, "hydrates": range_hydrates})
    observed = {}
    assert _select_chatgpt_model(
        page, "chromium", target, observed, thinking_effort=effort, session_type=session_type,
    )
    assert observed["observed"] == label
    assert observed["available_efforts"] == ["Instant", "Medium", "High", "Extra High"]
    assert observed["thinking_effort"] == "Extra High"
    assert observed["effort_catalog_complete"] is True
    assert page.locator('#power').get_attribute('aria-expanded') == 'false'


@pytest.fixture
def capability_page(disposable_browser_launch):
    playwright_sync = pytest.importorskip("playwright.sync_api")
    with playwright_sync.sync_playwright() as playwright:
        browser = disposable_browser_launch(
            playwright.chromium,
            headless=True,
            **({} if Path(playwright.chromium.executable_path).is_file() else {"channel": "chrome"}),
        )
        try:
            yield browser.new_page()
        finally:
            browser.close()


@pytest.mark.parametrize("reason,recover,attempts", [
    ("model-catalog-unavailable", True, 2),
    ("model-catalog-unavailable", False, 2),
    ("requested-effort-control-not-found", False, 1),
    ("effort-slider-not-found", True, 2),
    ("effort-slider-unreadable", True, 2),
    ("power-control-recycled", True, 2),
    ("effort-range-changed", True, 2),
    ("effort-slider-not-found", False, 2),
    ("effort-range-exceeds-safe-bound", False, 1),
])
def test_bootstrap_retries_transient_picker_failures_on_the_same_page(reason, recover, attempts):
    from app.core.chatgpt_agent_sources import _discover_chatgpt_agent_efforts

    page = object()
    visited = []

    def select(current_page, _browser, _model, observation, **_kwargs):
        visited.append(current_page)
        if recover and len(visited) == 2:
            observation.update(
                observed="Latest",
                thinking_effort="Maximum",
                available_efforts=["Measured", "Maximum"],
                effort_catalog_complete=True,
                model_options=chatgpt_live_catalog(["Latest"]),
                model_catalog_complete=True,
            )
            return True
        observation["reason"] = reason
        return False

    with patch("app.core.computer_use_agent._select_chatgpt_model", side_effect=select):
        status = _discover_chatgpt_agent_efforts(page)
    assert visited == [page] * attempts
    assert status["model_verified"] is recover
    assert status["effort_catalog_complete"] is recover
    if recover:
        assert status["actual_model"] == "Latest"
        assert status["available_efforts"] == ["Measured", "Maximum"]
    else:
        assert status["effort_catalog_error"] == reason
