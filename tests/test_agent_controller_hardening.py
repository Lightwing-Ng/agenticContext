"""Focused tests for controller hardening: model verification, action parser,
directory picker, recent-session catalog, and browser interruption recovery.

Code version: v3.50.2-codex.1
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

from app.core.browser_sessions import select_provider_tab
from app.core.computer_use_agent import (
    AgentRunSnapshot,
    CHATGPT_MODEL_VERIFICATION_ATTEMPTS,
    DEFAULT_CHATGPT_MODEL,
    DEFAULT_MACOS_SYSTEM_PROMPT,
    DEFAULT_WINDOWS_SYSTEM_PROMPT,
    MAX_BASE64_DECODED_BYTES,
    MAX_INVALID_ACTION_RETRIES,
    SAFE_PROTOCOL_PROMPT_MARKERS,
    WorkspaceController,
    _detect_browser_interruption,
    _run_web_action_loop,
    _select_chatgpt_model,
    parse_agent_action,
    ComputerUseSettings,
    ComputerUseSettingsStore,
    session_type_for_mode,
)
from app.web.app import (
    create_app,
    is_excluded_system_directory,
    validate_local_directory_path,
)


def _select_verified_chatgpt_model(*args: object, **kwargs: object) -> bool:
    """Model-selection stub that preserves ChatGPT's live effort proof gate."""
    observation = kwargs.get("observation")
    if observation is None:
        observation = next(
            (candidate for candidate in reversed(args) if isinstance(candidate, dict)),
            None,
        )
    if isinstance(observation, dict):
        observation.update(
            {
                "observed": "GPT-5.6 Sol",
                "thinking_effort": "Dynamic maximum",
                "available_efforts": ["Dynamic maximum"],
                "effort_catalog_complete": True,
            }
        )
    return True


# ---------------------------------------------------------------------------
# Helper mock page classes
# ---------------------------------------------------------------------------


class _VerifiedEffortSlider:
    """One trusted live slider with a bounded single-position catalog."""

    def count(self) -> int:
        return 1

    def nth(self, _index: int) -> "_VerifiedEffortSlider":
        return self

    def is_visible(self) -> bool:
        return True

    def get_attribute(self, name: str, **_kwargs: object) -> str | None:
        return {
            "aria-valuemin": "0",
            "aria-valuemax": "0",
            "aria-valuenow": "0",
            "aria-valuetext": "Dynamic maximum",
            "aria-label": "Dynamic maximum",
        }.get(name)

    def press(self, _key: str, **_kwargs: object) -> None:
        return None


class _FreshSessionPage:
    """Simulate a fresh chatgpt.com/ page where the model menu loads slowly."""

    def __init__(self, delay_until_attempt: int = 0, model_label: str = "GPT-5.6 Sol"):
        self._attempt = 0
        self._delay_until = delay_until_attempt
        self._model_label = model_label
        self.url = "https://chatgpt.com/"
        self.slider = _VerifiedEffortSlider()

    def evaluate(
        self,
        expression: str,
        argument: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if "expectedScope" in expression:
            return {"ok": True, "scope": "composer"}
        self._attempt += 1
        if self._attempt <= self._delay_until:
            return {"ok": False, "reason": "power-control-not-found", "available": []}
        return {
            "ok": True,
            "selected": self._model_label.lower(),
            "available": [self._model_label.lower()],
        }

    def locator(self, selector: str) -> object:
        if "data-cachelikes-effort-binding" in selector:
            return self.slider
        return _ChromiumTriggerPage._Empty()

    def wait_for_timeout(self, _ms: int) -> None:
        pass


class _ExistingSessionPage:
    """Simulate an existing /c/ session where verification succeeds immediately."""

    def __init__(self, model_label: str = "GPT-5.6 Sol"):
        self._model_label = model_label
        self.url = "https://chatgpt.com/c/abc123"
        self.slider = _VerifiedEffortSlider()

    def evaluate(
        self,
        expression: str,
        argument: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if "expectedScope" in expression:
            return {"ok": True, "scope": "composer"}
        return {
            "ok": True,
            "selected": self._model_label.lower(),
            "available": [self._model_label.lower()],
        }

    def locator(self, selector: str) -> object:
        if "data-cachelikes-effort-binding" in selector:
            return self.slider
        return _ChromiumTriggerPage._Empty()

    def wait_for_timeout(self, _ms: int) -> None:
        pass


class _WrongModelPage:
    """Model menu returns a different model than requested — script rejects it."""

    def __init__(self):
        self.url = "https://chatgpt.com/"

    def evaluate(self, expression: str, argument: dict[str, object]) -> dict[str, object]:
        return {
            "ok": False,
            "reason": "model-not-exposed",
            "current": "gpt-4o",
            "available": ["gpt-4o"],
        }

    def wait_for_timeout(self, _ms: int) -> None:
        pass


class _MissingMenuPage:
    """Model menu never loads."""

    def __init__(self):
        self.url = "https://chatgpt.com/"

    def evaluate(self, expression: str, argument: dict[str, object]) -> dict[str, object]:
        return {"ok": False, "reason": "power-control-not-found", "available": []}

    def wait_for_timeout(self, _ms: int) -> None:
        pass


class _ChromiumTriggerPage:
    """Chromium reused-session page whose model trigger uses a confirmed live label."""

    def __init__(self, trigger_name: str, current: str = "GPT-5.6 Sol"):
        self.trigger_name = trigger_name
        self.current = current
        self.url = "https://chatgpt.com/c/reused-session"
        self.expanded = False
        self.slider = _VerifiedEffortSlider()

    class _Empty:
        def count(self) -> int:
            return 0

    class _Trigger:
        def __init__(self, owner: "_ChromiumTriggerPage") -> None:
            self.owner = owner

        def count(self) -> int:
            return 1

        def nth(self, _index: int) -> "_ChromiumTriggerPage._Trigger":
            return self

        def is_visible(self) -> bool:
            return True

        def get_attribute(self, name: str) -> str | None:
            if name == "aria-expanded":
                return "true" if self.owner.expanded else "false"
            return None

        def click(self) -> None:
            self.owner.expanded = not self.owner.expanded

    def get_by_role(
        self,
        role: str,
        name: str | None = None,
        exact: bool | None = None,
    ) -> object:
        if role == "button" and name == self.trigger_name and exact is True:
            return self._Trigger(self)
        return self._Empty()

    def locator(self, selector: str) -> object:
        if "data-cachelikes-effort-binding" in selector:
            return self.slider
        return self._Empty()

    def evaluate(self, expression: str, *_args: object) -> dict[str, object]:
        if "expectedScope" in expression:
            return {"ok": True, "scope": "composer"}
        if "current:" in expression:
            selected_model = (
                self.current
                if self.current.casefold().startswith("gpt-")
                else "GPT-5.6 Sol"
            )
            return {
                "ok": True,
                "current": self.current,
                "selected_model": selected_model,
            }
        return {
            "buttons": [self.trigger_name],
            "candidate_buttons": [self.trigger_name],
            "menus": [],
        }

    def wait_for_timeout(self, _ms: int) -> None:
        pass


def _patch_action_loop_browser(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_ok: bool = True,
) -> dict[str, int]:
    import app.core.computer_use_agent as computer_use_agent

    calls = {"attach": 0, "submit": 0}
    monkeypatch.setattr(computer_use_agent, "_verify_agent_page", lambda *_args: None)
    monkeypatch.setattr(computer_use_agent, "_select_chat_mode", lambda *_args: None)
    def select_model(*args: object, **kwargs: object) -> bool:
        observation = kwargs.get("observation")
        if observation is None:
            observation = args[-1] if args else None
        if isinstance(observation, dict):
            observation.update(
                {
                    "observed": "GPT-5.6 Sol",
                    "thinking_effort": "Dynamic maximum",
                    "available_efforts": ["Dynamic maximum"],
                    "effort_catalog_complete": True,
                    "attempted_labels": ["GPT-5.6 Sol", "diagnostic-trigger"],
                    "visible_buttons": ["Instant"],
                }
            )
        return model_ok

    monkeypatch.setattr(computer_use_agent, "_select_web_model", select_model)

    def attach(*_args: object, **_kwargs: object) -> bool:
        calls["attach"] += 1
        return False

    def submit(*_args: object, **_kwargs: object) -> str:
        calls["submit"] += 1
        return '{"action":"bodycheck"}'

    monkeypatch.setattr(computer_use_agent, "_attach_context_file", attach)
    monkeypatch.setattr(computer_use_agent, "_submit_and_wait", submit)
    return calls


# ---------------------------------------------------------------------------
# 1. Model verification tests
# ---------------------------------------------------------------------------


class TestModelVerificationFreshSession:
    """Tests for fresh, reused, and project session model verification."""

    def test_named_retry_budget_defaults_to_three(self) -> None:
        assert CHATGPT_MODEL_VERIFICATION_ATTEMPTS == 3

    def test_session_mode_maps_explicitly_without_url_inference(self) -> None:
        assert session_type_for_mode("new") == "fresh"
        assert session_type_for_mode("recent") == "reused"
        assert session_type_for_mode("project_new") == "project"
        assert session_type_for_mode("project_session") == "project"

    def test_fresh_session_verification_succeeds_immediately(self) -> None:
        page = _FreshSessionPage(delay_until_attempt=0)
        assert _select_chatgpt_model(page, "chromium", DEFAULT_CHATGPT_MODEL) is True

    def test_fresh_session_verification_succeeds_after_menu_delay(self) -> None:
        page = _FreshSessionPage(delay_until_attempt=1)
        assert _select_chatgpt_model(page, "chromium", DEFAULT_CHATGPT_MODEL) is True

    def test_verification_reacquires_locators_on_every_retry(self) -> None:
        page = _FreshSessionPage(delay_until_attempt=2)
        assert _select_chatgpt_model(page, "chromium", DEFAULT_CHATGPT_MODEL) is True
        assert page._attempt >= CHATGPT_MODEL_VERIFICATION_ATTEMPTS

    def test_existing_session_verification_succeeds_immediately(self) -> None:
        page = _ExistingSessionPage()
        assert _select_chatgpt_model(page, "chromium", DEFAULT_CHATGPT_MODEL) is True

    def test_wrong_model_readback_returns_false(self) -> None:
        page = _WrongModelPage()
        assert _select_chatgpt_model(page, "chromium", DEFAULT_CHATGPT_MODEL) is False

    def test_missing_model_readback_returns_false(self) -> None:
        page = _MissingMenuPage()
        assert _select_chatgpt_model(page, "chromium", DEFAULT_CHATGPT_MODEL) is False

    @pytest.mark.parametrize("session_mode", ("new", "recent", "project_new", "project_session"))
    def test_no_context_or_prompt_before_chatgpt_verification(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        session_mode: str,
    ) -> None:
        calls = _patch_action_loop_browser(monkeypatch, model_ok=False)
        workspace = tmp_path / "project"
        workspace.mkdir()
        controller = WorkspaceController(
            workspace,
            ComputerUseSettings(workspace_path=str(workspace)),
            lambda: False,
        )
        with pytest.raises(RuntimeError, match="could not verify Best available") as exc_info:
            _run_web_action_loop(
                page=_FreshSessionPage() if session_mode in {"new", "project_new"} else _ExistingSessionPage(),
                browser_kind="chromium",
                initial_message="Inspect the project.",
                controller=controller,
                context_path=tmp_path / "context.md",
                settings=ComputerUseSettings(workspace_path=str(workspace)),
                session_mode=session_mode,
                selected_target_url=(
                    "https://chatgpt.com/"
                    if session_mode in {"new", "project_new"}
                    else "https://chatgpt.com/c/abc123"
                ),
                should_stop=lambda: False,
                update=lambda **_changes: None,
            )
        message = str(exc_info.value)
        assert "No project context or prompt was sent" in message
        assert f"session_mode={session_mode}" in message
        assert "expected_model=" in message
        assert "observed_model=" in message
        assert "attempted_labels=['GPT-5.6 Sol', 'diagnostic-trigger']" in message
        assert "visible_buttons=['Instant']" in message
        assert calls == {"attach": 0, "submit": 0}

    def test_reused_session_sol_trigger_verifies(self) -> None:
        page = _ChromiumTriggerPage("GPT-5.6 Sol")
        assert _select_chatgpt_model(page, "chromium", "gpt-5.6-sol") is True

    def test_reused_session_instant_trigger_verifies(self) -> None:
        page = _ChromiumTriggerPage("Instant")
        assert _select_chatgpt_model(page, "chromium", "gpt-5.6-sol") is True

    def test_reused_session_medium_trigger_verifies_extra_high(self) -> None:
        page = _ChromiumTriggerPage("Medium", current="Extra High")
        assert _select_chatgpt_model(page, "chromium", "gpt-5.6-sol") is True


# ---------------------------------------------------------------------------
# 2. Action parser tests
# ---------------------------------------------------------------------------


class TestActionParserFencedJSON:
    """Tests for fenced-block-first parsing."""

    def test_valid_fenced_json(self) -> None:
        response = '```json\n{"action":"list","path":"."}\n```'
        result = parse_agent_action(response)
        assert result["action"] == "list"
        assert result["path"] == "."

    def test_valid_pre_code_json(self) -> None:
        response = '<pre><code>{"action":"read","path":"test.py"}</code></pre>'
        result = parse_agent_action(response)
        assert result["action"] == "read"

    def test_escaped_quotes_in_fenced_json(self) -> None:
        response = '```json\n{"action":"replace","path":"f.py","old":"a=\\"1\\"","new":"b=\\"2\\""}\n```'
        result = parse_agent_action(response)
        assert result["action"] == "replace"
        assert result["old"] == 'a="1"'
        assert result["new"] == 'b="2"'

    def test_backslashes_in_fenced_json(self) -> None:
        response = '```json\n{"action":"write","path":"f.py","content":"line1\\\\nline2"}\n```'
        result = parse_agent_action(response)
        assert result["action"] == "write"
        assert "\\" in result["content"]

    def test_html_attributes_in_fenced_json(self) -> None:
        """Properly escaped HTML attributes should parse successfully."""
        response = '```json\n{"action":"replace","path":"t.html","old":" aria-describedby=\\"agent_status\\"","new":" aria-describedby=\\"agent_path_status\\""}\n```'
        result = parse_agent_action(response)
        assert result["action"] == "replace"
        assert 'aria-describedby="agent_status"' in result["old"]

    def test_malformed_raw_rendered_json_rejects(self) -> None:
        """Malformed JSON from HTML rendering (unescaped quotes) should raise an error."""
        response = '```json\n{"action":"replace","path":"t.html","old":" aria-describedby="true"","new":" aria-describedby="agent_status""}\n```'
        with pytest.raises(ValueError, match="not valid strict JSON|base64"):
            parse_agent_action(response)

    def test_multiple_fenced_blocks_with_different_actions_rejects(self) -> None:
        response = (
            '```json\n{"action":"read","path":"a.py"}\n```\n'
            '```json\n{"action":"write","path":"b.py","content":"x"}\n```'
        )
        with pytest.raises(ValueError, match="more than one"):
            parse_agent_action(response)

    def test_prose_outside_fence_is_ignored(self) -> None:
        response = 'Here is my action:\n```json\n{"action":"list","path":"."}\n```\nDone!'
        result = parse_agent_action(response)
        assert result["action"] == "list"

    def test_write_base64_action_parses(self) -> None:
        content = base64.b64encode(b'<div class="test">Hello</div>').decode()
        response = json.dumps({"action": "write_base64", "path": "t.html", "content_base64": content})
        result = parse_agent_action(response)
        assert result["action"] == "write_base64"
        assert result["content_base64"] == content

    def test_replace_base64_action_parses(self) -> None:
        old = base64.b64encode(b' aria-describedby="true"').decode()
        new = base64.b64encode(b' aria-describedby="agent_status"').decode()
        response = json.dumps({
            "action": "replace_base64",
            "path": "t.html",
            "old_base64": old,
            "new_base64": new,
        })
        result = parse_agent_action(response)
        assert result["action"] == "replace_base64"

    def test_raw_json_object_still_works(self) -> None:
        """Backward compatibility: bare JSON without fences still parses."""
        response = '{"action":"bodycheck"}'
        result = parse_agent_action(response)
        assert result["action"] == "bodycheck"

    def test_fenced_json_followed_by_later_bare_json_rejects_distinct_same_action(self) -> None:
        response = (
            '```json\n{"action":"read","path":"first.txt"}\n```\n'
            '{"action":"read","path":"final.txt"}'
        )
        with pytest.raises(ValueError, match="more than one"):
            parse_agent_action(response)

    def test_duplicate_keys_and_malformed_structured_blocks_reject(self) -> None:
        with pytest.raises(ValueError, match="duplicate JSON object key"):
            parse_agent_action(
                '{"action":"read","action":"write","path":"x.txt","content":"changed"}'
            )
        with pytest.raises(ValueError, match="structured JSON block"):
            parse_agent_action(
                '```json\n{"action":"write","path":"x.txt",}\n```\n'
                '{"action":"list","path":"."}'
            )
        with pytest.raises(ValueError, match="structured JSON block"):
            parse_agent_action(
                '```json\n{"action":"write","path":"x.txt",}\n'
                '{"action":"list","path":"."}'
            )
        with pytest.raises(ValueError, match="malformed JSON-like value"):
            parse_agent_action(
                'prefix {"wrapper":oops,"payload":'
                '{"action":"write","path":"x.txt","content":"changed"}} suffix'
            )

    def test_exact_duplicate_actions_are_safe_to_deduplicate(self) -> None:
        response = (
            '```json\n{"action":"read","path":"same.txt"}\n```\n'
            '{"action":"read","path":"same.txt"}'
        )
        assert parse_agent_action(response) == {
            "action": "read",
            "path": "same.txt",
        }

    def test_fenced_json_followed_by_later_bare_json_rejects_different_actions(self) -> None:
        response = (
            '```json\n{"action":"read","path":"first.txt"}\n```\n'
            '{"action":"bodycheck"}'
        )
        with pytest.raises(ValueError, match="more than one"):
            parse_agent_action(response)


class TestAriaDescribedbyRegression:
    """Regression test: the malformed aria-describedby action must not cause an infinite loop."""

    def test_malformed_aria_describedby_rejects_without_infinite_loop(self) -> None:
        """The exact malformed action from the bug report should raise immediately."""
        malformed = (
            '```json\n'
            '{"action":"replace","path":"app/web/templates/agent.html",'
            '"old":" aria-describedby="true"",'
            '"new":" aria-describedby="agent_project_path_status""}\n'
            '```'
        )
        with pytest.raises(ValueError, match="not valid strict JSON|base64"):
            parse_agent_action(malformed)

    def test_same_malformed_response_uses_the_bounded_escalating_retry_budget(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import app.core.computer_use_agent as computer_use_agent

        class _Page:
            url = "https://chatgpt.com/c/malformed"

        workspace = tmp_path / "project"
        workspace.mkdir()
        controller = WorkspaceController(
            workspace,
            ComputerUseSettings(workspace_path=str(workspace), max_turns=8),
            lambda: False,
        )
        malformed = (
            '```json\n'
            '{"action":"replace","path":"app/web/templates/agent.html",'
            '"old":" aria-describedby="true"",'
            '"new":" aria-describedby="agent_project_path_status""}\n'
            '```'
        )
        submitted: list[str] = []
        correction_timeouts: list[object] = []

        def submit(
            _page: object,
            _browser: str,
            message: str,
            _should_stop: object,
            **_kwargs: object,
        ) -> str:
            submitted.append(message)
            correction_timeouts.append(_kwargs.get("timeout_seconds"))
            return malformed

        monkeypatch.setattr(computer_use_agent, "_verify_agent_page", lambda *_args: None)
        monkeypatch.setattr(computer_use_agent, "_select_chat_mode", lambda *_args: None)
        monkeypatch.setattr(
            computer_use_agent,
            "_select_web_model",
            _select_verified_chatgpt_model,
        )
        monkeypatch.setattr(computer_use_agent, "_attach_context_file", lambda *_args: False)
        monkeypatch.setattr(computer_use_agent, "_submit_and_wait", submit)

        with pytest.raises(RuntimeError, match="too many invalid"):
            _run_web_action_loop(
                page=_Page(),
                browser_kind="chromium",
                initial_message="Fix the describedby attribute.",
                controller=controller,
                context_path=tmp_path / "context.md",
                settings=ComputerUseSettings(workspace_path=str(workspace), max_turns=8),
                session_mode="recent",
                selected_target_url="https://chatgpt.com/c/malformed",
                should_stop=lambda: False,
                update=lambda **_changes: None,
            )
        assert len(submitted) == MAX_INVALID_ACTION_RETRIES + 1
        assert "strict-format correction 1 of 3" in submitted[1]
        assert submitted[1].count("Return exactly one strict JSON controller action") == 1
        assert "You repeated the same invalid response" in submitted[2]
        final_correction = json.loads(submitted[3].splitlines()[1])
        assert '{"action":"list","path":".","depth":2}' in final_correction["instruction"]
        assert len(set(submitted[1:])) == MAX_INVALID_ACTION_RETRIES
        assert correction_timeouts == [None] * (MAX_INVALID_ACTION_RETRIES + 1)

    def test_repeated_malformed_response_can_recover_before_the_retry_limit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import app.core.computer_use_agent as computer_use_agent

        class _Page:
            url = "https://chatgpt.com/c/recovered"

        workspace = tmp_path / "project"
        workspace.mkdir()
        controller = WorkspaceController(
            workspace,
            ComputerUseSettings(workspace_path=str(workspace), max_turns=8),
            lambda: False,
        )
        malformed = "I should inspect several files before editing."
        responses = iter(
            (
                malformed,
                malformed,
                '{"action":"bodycheck"}',
                (
                    '{"action":"final","summary":"Recovered",'
                    '"verification":["bodycheck passed"],"limitations":[]}'
                ),
            )
        )
        submitted: list[str] = []

        def submit(
            _page: object,
            _browser: str,
            message: str,
            _should_stop: object,
            **_kwargs: object,
        ) -> str:
            submitted.append(message)
            return next(responses)

        monkeypatch.setattr(computer_use_agent, "_verify_agent_page", lambda *_args: None)
        monkeypatch.setattr(computer_use_agent, "_select_chat_mode", lambda *_args: None)
        monkeypatch.setattr(
            computer_use_agent,
            "_select_web_model",
            _select_verified_chatgpt_model,
        )
        monkeypatch.setattr(computer_use_agent, "_attach_context_file", lambda *_args: False)
        monkeypatch.setattr(computer_use_agent, "_submit_and_wait", submit)

        result = _run_web_action_loop(
            page=_Page(),
            browser_kind="chromium",
            initial_message="Inspect the project.",
            controller=controller,
            context_path=tmp_path / "context.md",
            settings=ComputerUseSettings(workspace_path=str(workspace), max_turns=8),
            session_mode="recent",
            selected_target_url="https://chatgpt.com/c/recovered",
            should_stop=lambda: False,
            update=lambda **_changes: None,
        )

        assert result == (
            "Recovered\n\nVerification\n- bodycheck passed",
            "https://chatgpt.com/c/recovered",
            2,
            True,
        )
        assert len(submitted) == 4
        assert "repeated_response" in submitted[2]

    @pytest.mark.parametrize("correction_delay", [180, 1_810])
    def test_correction_uses_the_normal_bounded_provider_wait(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        correction_delay: int,
    ) -> None:
        import app.core.computer_use_agent as computer_use_agent

        class _Page:
            url = "https://chatgpt.com/c/slow-correction"

        workspace = tmp_path / "project"
        workspace.mkdir()
        settings = ComputerUseSettings(workspace_path=str(workspace), max_turns=8)
        controller = WorkspaceController(workspace, settings, lambda: False)
        clock = 10_000.0
        submitted_at = clock
        submitted: list[str] = []
        updates: list[dict[str, object]] = []
        replies = [
            "I should inspect several files before editing.",
            '{"action":"bodycheck"}',
            '{"action":"final","summary":"Recovered",'
            '"verification":["bodycheck passed"],"limitations":[]}',
        ]

        def submit(_page: object, message: str, *_args: object, **_kwargs: object) -> None:
            nonlocal submitted_at
            submitted.append(message)
            submitted_at = clock

        def snapshot(*_args: object) -> dict[str, object]:
            generating = len(submitted) == 2 and clock - submitted_at < correction_delay
            receipt_marker = str(_args[2] if len(_args) > 2 else "")
            latest_user_text = submitted[-1] if submitted else ""
            return {
                "url": _Page.url,
                "count": len(submitted),
                "text": "" if not submitted or generating else replies[len(submitted) - 1],
                "generating": generating,
                "assistantAfterLatestUser": bool(submitted),
                "assistantMessageId": f"assistant-{len(submitted)}" if submitted else "",
                "userCount": len(submitted),
                "latestUserText": latest_user_text,
                "latestUserMessageId": f"user-{len(submitted)}" if submitted else "",
                "markerEchoed": bool(receipt_marker and receipt_marker in latest_user_text),
            }

        def wait(*_args: object) -> None:
            nonlocal clock
            clock += 30

        monkeypatch.setattr(computer_use_agent.time, "monotonic", lambda: clock)
        monkeypatch.setattr(computer_use_agent, "_web_wait", wait)
        monkeypatch.setattr(computer_use_agent, "_submit_chromium_prompt", submit)
        monkeypatch.setattr(computer_use_agent, "_chatgpt_response_snapshot", snapshot)
        monkeypatch.setattr(computer_use_agent, "_verify_agent_page", lambda *_args: None)
        monkeypatch.setattr(computer_use_agent, "_select_chat_mode", lambda *_args: None)
        monkeypatch.setattr(computer_use_agent, "_select_web_model", _select_verified_chatgpt_model)
        monkeypatch.setattr(computer_use_agent, "_attach_context_file", lambda *_args: False)

        def run() -> tuple[str, str, int, bool]:
            return _run_web_action_loop(
                page=_Page(),
                browser_kind="chromium",
                initial_message="Inspect the project.",
                controller=controller,
                context_path=tmp_path / "context.md",
                settings=settings,
                session_mode="recent",
                selected_target_url=_Page.url,
                should_stop=lambda: False,
                update=lambda **changes: updates.append(changes),
            )

        if correction_delay > computer_use_agent.WEB_TURN_TIMEOUT_SECONDS:
            with pytest.raises(RuntimeError, match="within 30 minutes"):
                run()
            assert len(submitted) == 2
            assert not controller.state.bodycheck_current
        else:
            result = run()
            assert result == (
                "Recovered\n\nVerification\n- bodycheck passed", _Page.url, 2, True
            )
            assert len(submitted) == 3
        assert "strict-format correction 1 of 3" in submitted[1]
        assert any("Provider is generating" in str(item.get("message")) for item in updates)

    def test_stop_wins_when_the_terminal_invalid_response_is_parsed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import app.core.computer_use_agent as computer_use_agent

        class _Page:
            url = "https://chatgpt.com/c/stopped-invalid"

        workspace = tmp_path / "project"
        workspace.mkdir()
        controller = WorkspaceController(
            workspace,
            ComputerUseSettings(workspace_path=str(workspace), max_turns=8),
            lambda: stop_state["requested"],
        )
        stop_state = {"requested": False}
        parse_calls = 0
        submitted: list[str] = []
        stopped: list[str] = []

        def parse(_response: str) -> dict[str, object]:
            nonlocal parse_calls
            parse_calls += 1
            if parse_calls == MAX_INVALID_ACTION_RETRIES + 1:
                stop_state["requested"] = True
            raise ValueError("invalid controller response")

        def submit(
            _page: object,
            _browser: str,
            message: str,
            _should_stop: object,
            **_kwargs: object,
        ) -> str:
            submitted.append(message)
            return "invalid"

        monkeypatch.setattr(computer_use_agent, "_verify_agent_page", lambda *_args: None)
        monkeypatch.setattr(computer_use_agent, "_select_chat_mode", lambda *_args: None)
        monkeypatch.setattr(
            computer_use_agent,
            "_select_web_model",
            _select_verified_chatgpt_model,
        )
        monkeypatch.setattr(computer_use_agent, "_attach_context_file", lambda *_args: False)
        monkeypatch.setattr(computer_use_agent, "_submit_and_wait", submit)
        monkeypatch.setattr(computer_use_agent, "parse_agent_action", parse)
        monkeypatch.setattr(
            computer_use_agent,
            "_stop_web_generation",
            lambda *_args: stopped.append("stopped"),
        )

        result = _run_web_action_loop(
            page=_Page(),
            browser_kind="chromium",
            initial_message="Inspect the project.",
            controller=controller,
            context_path=tmp_path / "context.md",
            settings=ComputerUseSettings(workspace_path=str(workspace), max_turns=8),
            session_mode="recent",
            selected_target_url="https://chatgpt.com/c/stopped-invalid",
            should_stop=lambda: stop_state["requested"],
            update=lambda **_changes: None,
        )

        assert result == (
            "",
            "https://chatgpt.com/c/stopped-invalid",
            0,
            False,
        )
        assert parse_calls == MAX_INVALID_ACTION_RETRIES + 1
        assert len(submitted) == MAX_INVALID_ACTION_RETRIES + 1
        assert stopped == ["stopped"]

    def test_stop_wins_when_a_valid_final_response_is_parsed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import app.core.computer_use_agent as computer_use_agent

        class _Page:
            url = "https://chatgpt.com/c/stopped-final"

        workspace = tmp_path / "project"
        workspace.mkdir()
        stop_state = {"requested": False}
        controller = WorkspaceController(
            workspace,
            ComputerUseSettings(workspace_path=str(workspace), max_turns=8),
            lambda: stop_state["requested"],
        )
        assert controller.execute({"action": "bodycheck"})["bodycheck_current"]
        submitted: list[str] = []
        stopped: list[str] = []

        def parse(_response: str) -> dict[str, object]:
            stop_state["requested"] = True
            return {
                "action": "final",
                "summary": "Published after Stop",
                "verification": ["bodycheck passed"],
                "limitations": [],
            }

        def submit(
            _page: object,
            _browser: str,
            message: str,
            _should_stop: object,
            **_kwargs: object,
        ) -> str:
            submitted.append(message)
            return "valid final"

        monkeypatch.setattr(computer_use_agent, "_verify_agent_page", lambda *_args: None)
        monkeypatch.setattr(computer_use_agent, "_select_chat_mode", lambda *_args: None)
        monkeypatch.setattr(
            computer_use_agent,
            "_select_web_model",
            _select_verified_chatgpt_model,
        )
        monkeypatch.setattr(computer_use_agent, "_attach_context_file", lambda *_args: False)
        monkeypatch.setattr(computer_use_agent, "_submit_and_wait", submit)
        monkeypatch.setattr(computer_use_agent, "parse_agent_action", parse)
        monkeypatch.setattr(
            computer_use_agent,
            "_stop_web_generation",
            lambda *_args: stopped.append("stopped"),
        )

        result = _run_web_action_loop(
            page=_Page(),
            browser_kind="chromium",
            initial_message="Inspect the project.",
            controller=controller,
            context_path=tmp_path / "context.md",
            settings=ComputerUseSettings(workspace_path=str(workspace), max_turns=8),
            session_mode="recent",
            selected_target_url="https://chatgpt.com/c/stopped-final",
            should_stop=lambda: stop_state["requested"],
            update=lambda **_changes: None,
        )

        assert result == ("", "https://chatgpt.com/c/stopped-final", 0, True)
        assert len(submitted) == 1
        assert stopped == ["stopped"]

    def test_final_completion_claim_rejects_a_later_stop_during_render(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import app.core.computer_use_agent as computer_use_agent

        class _Page:
            url = "https://chatgpt.com/c/completion-claimed"

        workspace = tmp_path / "project"
        workspace.mkdir()
        stop_signal = computer_use_agent._LinearizedStopSignal()
        controller = WorkspaceController(
            workspace,
            ComputerUseSettings(workspace_path=str(workspace), max_turns=8),
            stop_signal.is_set,
        )
        assert controller.execute({"action": "bodycheck"})["bodycheck_current"]
        submitted: list[str] = []
        stop_attempts: list[bool] = []

        def submit(
            _page: object,
            _browser: str,
            message: str,
            _should_stop: object,
            **_kwargs: object,
        ) -> str:
            submitted.append(message)
            return (
                '{"action":"final","summary":"Claimed final",'
                '"verification":["bodycheck passed"],"limitations":[]}'
            )

        def render(_action: dict[str, object]) -> str:
            stop_attempts.append(stop_signal.set())
            return "Claimed final\n\nVerification\n- bodycheck passed"

        monkeypatch.setattr(computer_use_agent, "_verify_agent_page", lambda *_args: None)
        monkeypatch.setattr(computer_use_agent, "_select_chat_mode", lambda *_args: None)
        monkeypatch.setattr(
            computer_use_agent,
            "_select_web_model",
            _select_verified_chatgpt_model,
        )
        monkeypatch.setattr(computer_use_agent, "_attach_context_file", lambda *_args: False)
        monkeypatch.setattr(computer_use_agent, "_submit_and_wait", submit)
        monkeypatch.setattr(computer_use_agent, "_render_final_action", render)

        result = _run_web_action_loop(
            page=_Page(),
            browser_kind="chromium",
            initial_message="Inspect the project.",
            controller=controller,
            context_path=tmp_path / "context.md",
            settings=ComputerUseSettings(workspace_path=str(workspace), max_turns=8),
            session_mode="recent",
            selected_target_url="https://chatgpt.com/c/completion-claimed",
            should_stop=stop_signal.is_set,
            update=lambda **_changes: None,
        )

        assert result == (
            "Claimed final\n\nVerification\n- bodycheck passed",
            "https://chatgpt.com/c/completion-claimed",
            1,
            True,
        )
        assert len(submitted) == 1
        assert stop_attempts == [False]
        assert not stop_signal.is_set()


# ---------------------------------------------------------------------------
# 3. Base64 action execution tests
# ---------------------------------------------------------------------------


class TestBase64Actions:
    """Test write_base64 and replace_base64 controller actions."""

    def test_write_base64_creates_file(self, tmp_path: Path) -> None:
        settings = ComputerUseSettings(workspace_path=str(tmp_path))
        controller = WorkspaceController(tmp_path, settings, lambda: False)
        content = '<div aria-describedby="status">Hello</div>'
        b64 = base64.b64encode(content.encode()).decode()
        result = controller.execute({
            "action": "write_base64",
            "path": "test.html",
            "content_base64": b64,
        })
        assert result["ok"] is True
        assert (tmp_path / "test.html").read_text() == content

    def test_replace_base64_modifies_file(self, tmp_path: Path) -> None:
        target = tmp_path / "test.html"
        target.write_text('<div aria-describedby="true">Hello</div>')
        settings = ComputerUseSettings(workspace_path=str(tmp_path))
        controller = WorkspaceController(tmp_path, settings, lambda: False)
        old = base64.b64encode(b' aria-describedby="true"').decode()
        new = base64.b64encode(b' aria-describedby="status"').decode()
        result = controller.execute({
            "action": "replace_base64",
            "path": "test.html",
            "old_base64": old,
            "new_base64": new,
        })
        assert result["ok"] is True
        assert 'aria-describedby="status"' in target.read_text()

    def test_replace_base64_allows_empty_new_value(self, tmp_path: Path) -> None:
        target = tmp_path / "test.html"
        target.write_text("keep-old-remove-tail")
        settings = ComputerUseSettings(workspace_path=str(tmp_path))
        controller = WorkspaceController(tmp_path, settings, lambda: False)
        old = base64.b64encode(b"-remove-tail").decode()
        result = controller.execute({
            "action": "replace_base64",
            "path": "test.html",
            "old_base64": old,
            "new_base64": "",
        })
        assert result["ok"] is True
        assert target.read_text() == "keep-old"

    def test_write_base64_invalid_encoding_fails(self, tmp_path: Path) -> None:
        settings = ComputerUseSettings(workspace_path=str(tmp_path))
        controller = WorkspaceController(tmp_path, settings, lambda: False)
        result = controller.execute({
            "action": "write_base64",
            "path": "bad.html",
            "content_base64": "not-valid-base64!!!",
        })
        assert result["ok"] is False
        assert "base64" in result.get("error", "").lower() or "Invalid" in result.get("error", "")

    def test_replace_base64_missing_old_fails(self, tmp_path: Path) -> None:
        target = tmp_path / "test.html"
        target.write_text("<div>Original</div>")
        settings = ComputerUseSettings(workspace_path=str(tmp_path))
        controller = WorkspaceController(tmp_path, settings, lambda: False)
        result = controller.execute({
            "action": "replace_base64",
            "path": "test.html",
            "old_base64": "",
            "new_base64": base64.b64encode(b"new").decode(),
        })
        assert result["ok"] is False

    def test_write_base64_rejects_oversized_payload(self, tmp_path: Path) -> None:
        settings = ComputerUseSettings(workspace_path=str(tmp_path))
        controller = WorkspaceController(tmp_path, settings, lambda: False)
        oversized = base64.b64encode(b"x" * (MAX_BASE64_DECODED_BYTES + 1)).decode()
        result = controller.execute({
            "action": "write_base64",
            "path": "huge.html",
            "content_base64": oversized,
        })
        assert result["ok"] is False
        assert "content_base64" in result.get("error", "")
        assert "160,000" in result.get("error", "")
        assert not (tmp_path / "huge.html").exists()


# ---------------------------------------------------------------------------
# 4. Directory picker tests (validation endpoint)
# ---------------------------------------------------------------------------


class TestDirectoryPickerValidation:
    """Test the /api/settings/directory/validate endpoint."""

    def test_valid_directory_returns_valid(self, tmp_path: Path) -> None:
        app = create_app()
        with app.test_client() as client:
            response = client.post(
                "/api/settings/directory/validate",
                json={"path": str(tmp_path)},
            )
            assert response.status_code == 200
            data = response.get_json()
            assert data["valid"] is True

    def test_nonexistent_path_returns_invalid(self, tmp_path: Path) -> None:
        app = create_app()
        with app.test_client() as client:
            response = client.post(
                "/api/settings/directory/validate",
                json={"path": str(tmp_path / "nonexistent_dir_xyz")},
            )
            assert response.status_code == 200
            data = response.get_json()
            assert data["valid"] is False
            assert "does not exist" in data["reason"]

    def test_file_path_returns_invalid(self, tmp_path: Path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("hello")
        app = create_app()
        with app.test_client() as client:
            response = client.post(
                "/api/settings/directory/validate",
                json={"path": str(f)},
            )
            assert response.status_code == 200
            data = response.get_json()
            assert data["valid"] is False
            assert "not a directory" in data["reason"]

    def test_empty_path_returns_invalid(self) -> None:
        app = create_app()
        with app.test_client() as client:
            response = client.post(
                "/api/settings/directory/validate",
                json={"path": ""},
            )
            assert response.status_code == 200
            data = response.get_json()
            assert data["valid"] is False

    def test_relative_path_returns_invalid(self) -> None:
        app = create_app()
        with app.test_client() as client:
            response = client.post(
                "/api/settings/directory/validate",
                json={"path": "relative/path"},
            )
            assert response.status_code == 200
            data = response.get_json()
            assert data["valid"] is False
            assert "absolute" in data["reason"]

    def test_system_directory_is_excluded(self) -> None:
        assert is_excluded_system_directory(Path("/System"))
        assert is_excluded_system_directory(Path("/"))
        if Path("/System").is_dir():
            valid, reason, _resolved = validate_local_directory_path("/System")
            assert valid is False
            assert "System directories" in reason

    def test_symlink_resolves_to_real_directory(self, tmp_path: Path) -> None:
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)
        valid, reason, resolved = validate_local_directory_path(str(link))
        assert valid is True
        assert reason == ""
        assert Path(resolved) == real_dir.resolve()

    def test_symlink_to_system_directory_is_excluded(self, tmp_path: Path) -> None:
        link = tmp_path / "system-link"
        link.symlink_to("/System")
        valid, reason, _resolved = validate_local_directory_path(str(link))
        assert valid is False
        assert "System directories" in reason

    def test_non_loopback_is_forbidden(self) -> None:
        app = create_app()
        with app.test_client() as client:
            response = client.post(
                "/api/settings/directory/validate",
                json={"path": "/tmp"},
                environ_base={"REMOTE_ADDR": "8.8.8.8"},
            )
            assert response.status_code == 403

    def test_browser_script_keeps_input_editable_and_reports_errors(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "app/web/static/settings-directory-picker.js"
        ).read_text(encoding="utf-8")
        for fragment in (
            'input.removeAttribute("readonly")',
            "AbortController",
            "/api/settings/directory/validate",
            "The server returned a malformed response.",
            "The folder picker did not respond. You can type the path directly.",
            "Selection cancelled.",
            "Path validation timed out",
            'input.setAttribute("aria-invalid", "true")',
            "clearLoadingState",
        ):
            assert fragment in script
        assert 'renderStatus("", false);' in script
        assert 'renderStatus("Path validated.", false);' not in script
        assert "silently ignore" not in script


# ---------------------------------------------------------------------------
# 5. Recent-session catalog tests
# ---------------------------------------------------------------------------


class TestRecentSessionCatalog:
    """Tests for catalog state management, timeout, and forced refresh."""

    def test_catalog_script_has_real_loading_ready_and_error_transitions(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "app/web/static/computer-use-agent.js"
        ).read_text(encoding="utf-8")
        assert 'catalogState = "idle"' in script
        assert 'catalogState = "loading"' in script
        assert 'catalogState = "ready"' in script
        assert 'catalogState = "error"' in script
        assert "CATALOG_TIMEOUT_MS = 15000" in script
        assert "new AbortController()" in script
        assert 'query.set("refresh", "1")' in script
        assert "loadAgentSources({forceRefresh: true})" in script
        assert 'catalogState === "error"' in script
        assert "clearCatalogLoadingState" in script
        assert "Recent sessions timed out after 15 seconds." in script

    def test_explicit_source_catalog_refresh_collects_the_newest_catalog(self) -> None:
        first_payload = {
            "platform": "gemini",
            "browser_label": "Edge",
            "recent_sessions": [
                {
                    "id": "first-session",
                    "title": "First session",
                    "url": "https://gemini.google.com/app/first-session",
                    "updated_at": "",
                }
            ],
            "projects": [],
            "limit": 20,
        }
        with TemporaryDirectory() as raw_root:
            app = create_app(Path(raw_root) / "local_store")
            with patch(
                "app.web.app.list_agent_sources",
                return_value=first_payload,
            ) as sources:
                with app.test_client() as client:
                    first_response = client.get("/api/agent/sources?platform=gemini&browser=edge")
                    query_response = client.get(
                        "/api/agent/sources?platform=gemini&browser=edge&refresh=1"
                    )
        assert first_response.get_json()["recent_sessions"] == []
        assert first_response.get_json()["cache"]["status"] == "unprobed"
        assert query_response.get_json()["recent_sessions"] == first_payload["recent_sessions"]
        assert query_response.get_json()["cache"]["status"] == "miss"
        sources.assert_called_once()

    def test_post_session_catalog_refresh_uses_refresh_query(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "app/web/static/computer-use-agent.js"
        ).read_text(encoding="utf-8")
        bind_index = script.index("function bindCompletedAgentSession")
        bind_chunk = script[bind_index:bind_index + 1_800]
        assert "automaticSourcesSuppressedAfterCompletion = true;" in bind_chunk
        assert "loadAgentSources({forceRefresh: true})" not in bind_chunk
        assert "if (!completedTransition" in bind_chunk
        assert "function runSupersedes(" in script
        assert "function agentRunRevision(" in script
        assert "if (runRevision && previousRevision)" in script
        assert "if (previousRevision || !startedAt) return false;" in script
        assert "const incomingRunIsStale" in script
        assert "if (incomingRunIsStale) return;" in script
        assert "lastRenderedAgentRunning === true && sameRenderedRun" in script
        assert "|| pendingRunConfirmed" in script
        assert "bindCompletedAgentSession(agent, completedTransition)" in script
        assert 'query.set("refresh", "1")' in script
        assert 'elements.sessionMode.value = "recent"' not in bind_chunk
        assert 'elements.sessionMode.value = "project"' not in bind_chunk
        assert "selectSessionListValue(" not in bind_chunk
        assert "sessionTitleOverride =" not in bind_chunk

    def test_catalog_tab_reconciliation_never_calls_bring_to_front(self) -> None:
        chatgpt = MagicMock()
        chatgpt.is_closed.return_value = False
        chatgpt.url = "https://chatgpt.com/"
        chatgpt.title.return_value = "ChatGPT"
        chatgpt.bring_to_front = MagicMock()
        other = MagicMock()
        other.is_closed.return_value = False
        other.url = "https://127.0.0.1:8666/agent/edge/chatgpt"
        other.title.return_value = "agenticContext Agent"
        context = MagicMock()
        context.pages = [other, chatgpt]
        chosen = select_provider_tab(
            context,
            home_url="https://chatgpt.com/",
            hosts={"chatgpt.com", "www.chatgpt.com"},
            title="ChatGPT",
        )
        assert chosen is chatgpt
        chatgpt.bring_to_front.assert_not_called()
        other.bring_to_front.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Browser interruption tests
# ---------------------------------------------------------------------------


class TestBrowserInterruption:
    """Tests for browser interruption detection and recovery."""

    def test_closed_page_detected(self) -> None:
        page = MagicMock()
        page.is_closed.return_value = True
        interrupted, reason = _detect_browser_interruption(page, "https://chatgpt.com/", "chromium")
        assert interrupted is True
        assert "closed" in reason.lower()

    def test_normal_page_not_interrupted(self) -> None:
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://chatgpt.com/"
        page.title.return_value = "ChatGPT"
        with patch("app.core.computer_use_agent._macos_screen_is_locked", return_value=False):
            interrupted, reason = _detect_browser_interruption(
                page, "https://chatgpt.com/", "chromium", platform="chatgpt", session_mode="new"
            )
        assert interrupted is False
        assert reason == ""

    def test_tab_navigated_away_detected(self) -> None:
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://google.com/search"
        page.title.return_value = "Google"
        interrupted, reason = _detect_browser_interruption(
            page, "https://chatgpt.com/", "chromium"
        )
        assert interrupted is True
        assert "navigated" in reason.lower()

    def test_page_exception_detected_as_interruption(self) -> None:
        page = MagicMock()
        page.is_closed.side_effect = RuntimeError("Page crashed")
        interrupted, reason = _detect_browser_interruption(page, "https://chatgpt.com/", "chromium")
        assert interrupted is True
        assert "accessible" in reason.lower() or "crashed" in reason.lower()

    def test_fresh_chatgpt_home_to_conversation_is_allowed(self) -> None:
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://chatgpt.com/c/new-session"
        page.title.return_value = "ChatGPT"
        with patch("app.core.computer_use_agent._macos_screen_is_locked", return_value=False):
            interrupted, reason = _detect_browser_interruption(
                page,
                "https://chatgpt.com/",
                "chromium",
                platform="chatgpt",
                session_mode="new",
            )
        assert interrupted is False
        assert reason == ""

    def test_fresh_grok_home_to_conversation_is_allowed(self) -> None:
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://grok.com/c/new-session"
        page.title.return_value = "Grok"
        with patch("app.core.computer_use_agent._macos_screen_is_locked", return_value=False):
            interrupted, reason = _detect_browser_interruption(
                page,
                "https://grok.com/",
                "chromium",
                platform="grok",
                session_mode="new",
            )
        assert interrupted is False
        assert reason == ""

    def test_grok_project_session_switch_is_detected(self) -> None:
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://grok.com/project/project-1?chat=session-2"
        page.title.return_value = "Grok"
        interrupted, reason = _detect_browser_interruption(
            page,
            "https://grok.com/project/project-1?chat=session-1",
            "chromium",
            platform="grok",
            session_mode="project_session",
        )
        assert interrupted is True
        assert "navigated" in reason.lower()

    def test_canonical_grok_url_drift_does_not_turn_a_title_update_into_an_interruption(
        self,
    ) -> None:
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://www.grok.com/c/session-1/?message=latest#response"
        page.title.return_value = "Updated conversation title"

        with patch(
            "app.core.computer_use_agent._macos_screen_is_locked",
            return_value=False,
        ):
            interrupted, reason = _detect_browser_interruption(
                page,
                "https://grok.com/c/session-1",
                "chromium",
                platform="grok",
                session_mode="recent",
                expected_title="Original conversation title",
            )

        assert interrupted is False
        assert reason == ""

    def test_edge_frontmost_alone_is_not_user_takeover(self) -> None:
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://chatgpt.com/"
        page.title.return_value = "ChatGPT"
        with patch("app.core.computer_use_agent._macos_screen_is_locked", return_value=False):
            with patch("app.core.computer_use_agent.subprocess.run") as run:
                run.return_value.stdout = "Microsoft Edge"
                interrupted, reason = _detect_browser_interruption(
                    page, "https://chatgpt.com/", "chromium", platform="chatgpt", session_mode="new"
                )
        assert interrupted is False
        assert reason == ""

    def test_unknown_lock_screen_does_not_pause(self) -> None:
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://chatgpt.com/"
        page.title.return_value = "ChatGPT"
        with patch("app.core.computer_use_agent._macos_screen_is_locked", return_value=None):
            interrupted, reason = _detect_browser_interruption(
                page, "https://chatgpt.com/", "chromium", platform="chatgpt", session_mode="new"
            )
        assert interrupted is False
        assert reason == ""

    def test_user_takeover_detected_when_session_url_changes(self) -> None:
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://chatgpt.com/c/other-session"
        page.title.return_value = "Something else"
        interrupted, reason = _detect_browser_interruption(
            page,
            "https://chatgpt.com/c/selected-session",
            "chromium",
            platform="chatgpt",
            session_mode="recent",
        )
        assert interrupted is True
        assert "navigated" in reason.lower() or "session" in reason.lower()

    def test_tab_identity_change_is_an_interruption(self) -> None:
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://chatgpt.com/c/selected-session"
        page.title.return_value = "ChatGPT"
        page._guid = "tab-b"
        interrupted, reason = _detect_browser_interruption(
            page,
            "https://chatgpt.com/c/selected-session",
            "chromium",
            platform="chatgpt",
            session_mode="recent",
            expected_tab_id="tab-a",
        )
        assert interrupted is True
        assert "identity" in reason.lower()

    def test_paused_snapshot_fields(self) -> None:
        snapshot = AgentRunSnapshot(
            paused=True,
            pause_reason="The selected provider tab was closed.",
        )
        assert snapshot.paused is True
        assert snapshot.pause_reason == "The selected provider tab was closed."

    def test_recovery_does_not_duplicate_submit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import app.core.computer_use_agent as computer_use_agent

        class _Page:
            url = "https://chatgpt.com/c/recovery"

        workspace = tmp_path / "project"
        workspace.mkdir()
        controller = WorkspaceController(
            workspace,
            ComputerUseSettings(workspace_path=str(workspace), max_turns=4),
            lambda: False,
        )
        responses = iter(
            (
                '{"action":"bodycheck"}',
                '{"action":"final","summary":"Done."}',
            )
        )
        submitted: list[str] = []
        detect_calls = {"n": 0}

        def detect(*_args: object, **_kwargs: object) -> tuple[bool, str]:
            detect_calls["n"] += 1
            if detect_calls["n"] == 1:
                return True, "The selected provider tab was closed."
            return False, ""

        def submit(
            _page: object,
            _browser: str,
            message: str,
            _should_stop: object,
            **_kwargs: object,
        ) -> str:
            submitted.append(message)
            return next(responses)

        monkeypatch.setattr(computer_use_agent, "_verify_agent_page", lambda *_args: None)
        monkeypatch.setattr(computer_use_agent, "_select_chat_mode", lambda *_args: None)
        monkeypatch.setattr(
            computer_use_agent,
            "_select_web_model",
            _select_verified_chatgpt_model,
        )
        monkeypatch.setattr(computer_use_agent, "_attach_context_file", lambda *_args: False)
        monkeypatch.setattr(computer_use_agent, "_submit_and_wait", submit)
        monkeypatch.setattr(computer_use_agent, "_detect_browser_interruption", detect)
        monkeypatch.setattr(
            computer_use_agent,
            "_wait_for_browser_recovery",
            lambda **_kwargs: "recovered",
        )

        result = _run_web_action_loop(
            page=_Page(),
            browser_kind="chromium",
            initial_message="Inspect the project.",
            controller=controller,
            context_path=tmp_path / "context.md",
            settings=ComputerUseSettings(workspace_path=str(workspace), max_turns=4),
            session_mode="recent",
            selected_target_url="https://chatgpt.com/c/recovery",
            should_stop=lambda: False,
            update=lambda **_changes: None,
        )
        assert result[0] == "Done."
        assert len(submitted) == 2

    def test_interruption_timeout_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import app.core.computer_use_agent as computer_use_agent

        class _Page:
            url = "https://chatgpt.com/c/timeout"

        workspace = tmp_path / "project"
        workspace.mkdir()
        controller = WorkspaceController(
            workspace,
            ComputerUseSettings(workspace_path=str(workspace), max_turns=4),
            lambda: False,
        )
        monkeypatch.setattr(computer_use_agent, "_verify_agent_page", lambda *_args: None)
        monkeypatch.setattr(computer_use_agent, "_select_chat_mode", lambda *_args: None)
        monkeypatch.setattr(
            computer_use_agent,
            "_select_web_model",
            _select_verified_chatgpt_model,
        )
        monkeypatch.setattr(computer_use_agent, "_attach_context_file", lambda *_args: False)
        monkeypatch.setattr(
            computer_use_agent,
            "_submit_and_wait",
            lambda *_args, **_kwargs: '{"action":"bodycheck"}',
        )
        monkeypatch.setattr(
            computer_use_agent,
            "_detect_browser_interruption",
            lambda *_args, **_kwargs: (True, "The selected provider tab was closed."),
        )
        monkeypatch.setattr(
            computer_use_agent,
            "_wait_for_browser_recovery",
            lambda **_kwargs: "timeout",
        )
        with pytest.raises(RuntimeError, match="did not recover"):
            _run_web_action_loop(
                page=_Page(),
                browser_kind="chromium",
                initial_message="Inspect the project.",
                controller=controller,
                context_path=tmp_path / "context.md",
                settings=ComputerUseSettings(workspace_path=str(workspace), max_turns=4),
                session_mode="recent",
                selected_target_url="https://chatgpt.com/c/timeout",
                should_stop=lambda: False,
                update=lambda **_changes: None,
            )


# ---------------------------------------------------------------------------
# 7. Snapshot field tests
# ---------------------------------------------------------------------------


class TestSnapshotFields:
    """Verify new snapshot fields are present and have correct defaults."""

    def test_session_type_default(self) -> None:
        snapshot = AgentRunSnapshot()
        assert snapshot.session_type == ""

    def test_catalog_fields_default(self) -> None:
        snapshot = AgentRunSnapshot()
        assert snapshot.catalog_state == "idle"
        assert snapshot.catalog_error == ""

    def test_pause_fields_default(self) -> None:
        snapshot = AgentRunSnapshot()
        assert snapshot.paused is False
        assert snapshot.pause_reason == ""

    def test_browser_constraint_is_edge(self) -> None:
        """Default browser must be edge to enforce the Edge constraint."""
        snapshot = AgentRunSnapshot()
        assert snapshot.browser == "edge"


# ---------------------------------------------------------------------------
# 8. Persisted prompt migration and status API
# ---------------------------------------------------------------------------


class TestPersistedPromptMigration:
    """Legacy persisted prompts must be rewritten to the current safe protocol."""

    def test_status_api_exposes_migrated_prompt_markers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "Kept Project"
        workspace.mkdir()
        settings_path = tmp_path / "computer-use-agent.json"
        settings_path.write_text(
            json.dumps(
                {
                    "workspace_path": str(workspace),
                    "operating_system": "macos",
                    "platform": "chatgpt",
                    "browser": "edge",
                    "model": "gpt-5.6-sol",
                    "target_url": "https://chatgpt.com/",
                    "context_limit_mib": 8,
                    "max_turns": 40,
                    "command_timeout_seconds": 120,
                    "macos_system_prompt": "Legacy macOS prompt without fenced JSON.",
                    "windows_system_prompt": "Legacy Windows prompt without base64 actions.",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "app.core.computer_use_agent.DEFAULT_AGENT_SETTINGS_PATH",
            settings_path,
        )
        app = create_app(tmp_path / "local_store")
        with app.test_client() as client:
            response = client.get("/api/agent/status")
        assert response.status_code == 200
        runtime_settings = response.get_json()["runtime"]["settings"]
        for marker in SAFE_PROTOCOL_PROMPT_MARKERS:
            assert marker in runtime_settings["macos_system_prompt"]
            assert marker in runtime_settings["windows_system_prompt"]
        persisted = json.loads(settings_path.read_text(encoding="utf-8"))
        for marker in SAFE_PROTOCOL_PROMPT_MARKERS:
            assert marker in persisted["macos_system_prompt"]
            assert marker in persisted["windows_system_prompt"]
        restarted = ComputerUseSettingsStore(settings_path)
        assert restarted.settings.macos_system_prompt == DEFAULT_MACOS_SYSTEM_PROMPT
        assert restarted.settings.windows_system_prompt == DEFAULT_WINDOWS_SYSTEM_PROMPT

    def test_status_api_upgrades_marker_complete_literal_search_contract(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "Kept Project"
        workspace.mkdir()
        settings_path = tmp_path / "computer-use-agent.json"
        legacy_action = (
            '{"action":"search","query":"text or regex","path":".",'
            '"glob":"*.py","max_results":80}'
        )
        current_action = (
            '{"action":"search","query":"literal text","path":".",'
            '"glob":"*.py","max_results":80}'
        )
        literal_instruction = (
            "Search action queries are literal text, never regular expressions."
        )
        legacy_prompt = DEFAULT_MACOS_SYSTEM_PROMPT.replace(
            current_action,
            legacy_action,
        ).replace(f"\n\n{literal_instruction}", "")
        custom_text = "Preserve this status-visible custom guidance."
        custom_windows_text = "Preserve this Windows status-visible guidance."
        legacy_windows_prompt = (
            "Return one action in a fenced code block labelled json. "
            "Use replace_base64 and write_base64 when needed."
        )
        settings_path.write_text(
            json.dumps(
                {
                    "workspace_path": str(workspace),
                    "operating_system": "macos",
                    "platform": "chatgpt",
                    "browser": "edge",
                    "model": "gpt-5.6-sol",
                    "target_url": "https://chatgpt.com/",
                    "context_limit_mib": 8,
                    "max_turns": 40,
                    "command_timeout_seconds": 120,
                    "macos_system_prompt": f"{legacy_prompt}\n\n{custom_text}",
                    "windows_system_prompt": (
                        f"{legacy_windows_prompt}\n\n{custom_windows_text}"
                    ),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "app.core.computer_use_agent.DEFAULT_AGENT_SETTINGS_PATH",
            settings_path,
        )

        app = create_app(tmp_path / "local_store")
        with app.test_client() as client:
            response = client.get("/api/agent/status")

        assert response.status_code == 200
        runtime_prompt = response.get_json()["runtime"]["settings"][
            "macos_system_prompt"
        ]
        assert current_action in runtime_prompt
        assert legacy_action not in runtime_prompt
        assert literal_instruction in runtime_prompt
        assert custom_text in runtime_prompt
        runtime_windows_prompt = response.get_json()["runtime"]["settings"][
            "windows_system_prompt"
        ]
        for marker in SAFE_PROTOCOL_PROMPT_MARKERS:
            assert marker in runtime_windows_prompt
        assert custom_windows_text in runtime_windows_prompt
        persisted_prompt = json.loads(
            settings_path.read_text(encoding="utf-8")
        )["macos_system_prompt"]
        assert persisted_prompt == runtime_prompt
