"""Keep each juror in one authenticated browser conversation for an entire question.

Code version: v1.1.3-codex.1
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import json
import re
import secrets
from threading import Event, get_ident
import time
from typing import Any, Callable

from . import computer_use_agent as web_agent
from .browser_sessions import (
    CHROMIUM_WINDOW_MODE_OFFSCREEN,
    CHROMIUM_WINDOW_MODE_TASK_STAGE,
    browser_descriptors,
    goto_with_retry,
    launch_chromium_context,
    sync_playwright_or_error,
)
from .config import CrawlConfig, is_macos_host
from .computer_use_agent import ComputerUseSettings


JURY_MODEL_OPTIONS_BY_PROVIDER: dict[str, tuple[dict[str, Any], ...]] = {
    "chatgpt": (
        {
            "selection_key": "chatgpt-latest-extra-high",
            "key": "live:latest",
            "label": "Latest",
            "display_label": "Latest · Extra High",
            "ui_label": "Latest",
            "remote_labels": ("Latest",),
            "chatgpt_effort": "Extra High",
        },
        {
            "selection_key": "chatgpt-latest-high",
            "key": "live:latest",
            "label": "Latest",
            "display_label": "Latest · High",
            "ui_label": "Latest",
            "remote_labels": ("Latest",),
            "chatgpt_effort": "High",
        },
        {
            "selection_key": "chatgpt-latest-medium",
            "key": "live:latest",
            "label": "Latest",
            "display_label": "Latest · Medium",
            "ui_label": "Latest",
            "remote_labels": ("Latest",),
            "chatgpt_effort": "Medium",
        },
        {
            "selection_key": "chatgpt-latest-instant",
            "key": "live:latest",
            "label": "Latest",
            "display_label": "Latest · Instant",
            "ui_label": "Latest",
            "remote_labels": ("Latest",),
            "chatgpt_effort": "Instant",
        },
    ),
    "grok": (
        {
            "selection_key": "grok-auto",
            "key": "grok-auto",
            "label": "Auto",
            "display_label": "Auto",
            "ui_label": "Auto",
            "remote_labels": ("Auto",),
        },
        {
            "selection_key": "grok-build",
            "key": "grok-build",
            "label": "Build",
            "display_label": "Build",
            "ui_label": "Build",
            "remote_labels": ("Build",),
            "remote_trigger_labels": ("Build Beta",),
        },
    ),
    "gemini": (
        {
            "selection_key": "gemini-3.1-pro",
            "key": "gemini-3.1-pro",
            "label": "Gemini 3.1 Pro",
            "display_label": "3.1 Pro",
            "ui_label": "3.1 Pro",
            "remote_labels": ("Gemini 3.1 Pro", "3.1 Pro"),
        },
        {
            "selection_key": "gemini-3.8-flash",
            "key": "gemini-3.8-flash",
            "label": "Gemini 3.8 Flash",
            "display_label": "3.8 Flash",
            "ui_label": "3.8 Flash",
            "remote_labels": ("Gemini 3.8 Flash", "3.8 Flash"),
        },
    ),
    "claude": (
        {
            "selection_key": "claude-auto",
            "key": "claude-auto",
            "label": "Auto",
            "display_label": "Auto",
            "ui_label": "Auto",
            "remote_labels": ("Auto",),
        },
    ),
}
JURY_MODEL_OPTIONS: dict[str, dict[str, Any]] = {
    provider: options[0]
    for provider, options in JURY_MODEL_OPTIONS_BY_PROVIDER.items()
}


def jury_model_option(platform: str, selection_key: object = None) -> dict[str, Any]:
    """Resolve one explicit Jury model tier, defaulting only when omitted."""
    options = JURY_MODEL_OPTIONS_BY_PROVIDER.get(platform, ())
    if selection_key is None:
        if not options:
            raise ValueError(f"Unsupported jury provider: {platform}")
        return dict(options[0])
    if not isinstance(selection_key, str):
        raise ValueError(f"Choose a supported model tier for {platform}.")
    selected = next(
        (option for option in options if option["selection_key"] == selection_key),
        None,
    )
    if selected is None:
        raise ValueError(f"Choose a supported model tier for {platform}.")
    return dict(selected)


class JuryBrowserStopped(RuntimeError):
    """The local stop signal canceled the juror's browser exchange."""


def normalize_jury_response(response: str, platform: str) -> str:
    """Remove only Grok's observed timing/source-count envelope around valid JSON."""
    if platform != "grok":
        return response
    match = re.fullmatch(
        r"Worked for [0-9]+(?:\.[0-9]+)?s\s*\n\s*\n"
        r"(?P<body>.+?)\s*\n\s*\n[0-9][0-9,]* sources?",
        response.strip(),
        flags=re.DOTALL,
    )
    if match is None:
        return response
    body = match.group("body").strip()
    json_body = body
    fence = re.fullmatch(r"```json\s*\n(?P<body>.+?)\n```", body, flags=re.DOTALL)
    if fence is not None:
        json_body = fence.group("body").strip()
    try:
        value = json.loads(json_body)
    except (ValueError, TypeError):
        return response
    return json_body if isinstance(value, dict) else response


def _validate_provider(settings: ComputerUseSettings, platform: str) -> None:
    if platform not in JURY_MODEL_OPTIONS:
        raise ValueError(f"Unsupported jury provider: {platform}")
    if settings.browser not in {"edge", "chrome"}:
        raise ValueError("Jury requires Microsoft Edge or Google Chrome.")


def jury_browser_login_check(
    settings: ComputerUseSettings,
    platform: str,
    *,
    config: CrawlConfig | None = None,
    model_selection: str | None = None,
) -> dict[str, Any]:
    """Check the selected provider in its headed Jury browser without a prompt."""
    _validate_provider(settings, platform)
    with JuryBrowserSession(
        settings,
        platform,
        config=config,
        model_selection=model_selection,
    ):
        return {
            "platform": platform,
            "browser": settings.browser,
            "logged_in": True,
            "ready": True,
            "can_download": False,
            "message": f"{platform} chat is signed in and ready.",
        }


class JuryBrowserSession:
    """Own one browser page on one worker thread, with no workspace controller.

    The coordinator must enter, ask, and exit on the same worker thread. A
    failed exchange closes admission to further prompts; it never creates a
    replacement conversation or resends an ambiguous submission.
    """

    def __init__(
        self,
        settings: ComputerUseSettings,
        platform: str,
        stop_event: Event | None = None,
        *,
        config: CrawlConfig | None = None,
        on_conversation: Callable[[str], None] | None = None,
        model_selection: str | None = None,
    ) -> None:
        _validate_provider(settings, platform)
        self.platform = platform
        self.model_option = jury_model_option(platform, model_selection)
        self.settings = replace(
            settings,
            platform=platform,
            model=self.model_option["key"],
            chatgpt_effort=str(self.model_option.get("chatgpt_effort") or "Extra High"),
            target_url=web_agent._platform_home_url(platform),
        )
        self.config = config or CrawlConfig()
        self.on_conversation = on_conversation
        self.stop_event = stop_event or Event()
        self.model_observation: dict[str, Any] = {}
        self.model_observation_history: list[dict[str, Any]] = []
        self.last_raw_response = ""
        self._resources: ExitStack | None = None
        self._page: Any = None
        self._binding: Any = None
        self._owner_thread: int | None = None
        self._entered = False
        self._failed = False
        self._turn_count = 0
        self._conversation_url = ""

    @property
    def conversation_url(self) -> str:
        """Return the last verified conversation identity without browser I/O."""
        return self._conversation_url

    def _require_running(self) -> None:
        if self.stop_event.is_set():
            raise JuryBrowserStopped("The jury was stopped.")

    def _require_owner(self) -> None:
        if self._owner_thread != get_ident():
            raise RuntimeError("A jury browser session must stay on its owning worker thread.")

    def _availability_check(self) -> bool:
        self._require_running()
        reason = web_agent._provider_human_verification_reason(self._page, self.platform)
        if reason:
            result = web_agent._wait_for_browser_recovery(
                page=self._page,
                expected_url=self._conversation_url or self.settings.target_url,
                browser_kind=self.settings.browser,
                platform=self.platform,
                session_mode="recent" if self._conversation_url else "new",
                expected_tab_id=None,
                expected_title="",
                should_stop=self.stop_event.is_set,
                should_resume=None,
                update=lambda **_changes: None,
                reason=reason,
                monitor_screen_lock=False,
            )
            if result == "stopped":
                raise JuryBrowserStopped("The jury was stopped during human verification.")
            self._require_running()
        return True

    def _record_response_state(self, **_state: Any) -> None:
        conversation_url = str(self._binding.bound_conversation_url or "")
        if conversation_url and conversation_url != self._conversation_url:
            self._conversation_url = conversation_url
            if self.on_conversation is not None:
                self.on_conversation(conversation_url)

    def _recover_response_session(self, receipt_marker: str, should_stop=None) -> str:
        """Wait on Gemini's accepted first turn without reopening or resending it."""
        deadline = time.monotonic() + 60
        while True:
            self._require_running()
            if callable(should_stop) and should_stop():
                raise JuryBrowserStopped("The jury was stopped.")
            conversation = self._binding.ensure_response_session(should_stop)
            if conversation or self.platform != "gemini" or self._turn_count:
                self._record_response_state()
                return conversation
            snapshot = web_agent._provider_turn_snapshot(
                self._page, self.platform, receipt_marker=receipt_marker,
            )
            if not snapshot.get("markerEchoed"):
                return ""
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Gemini received this question but did not expose its conversation "
                    "URL within 60 seconds. The message was not resent."
                )
            self._availability_check()
            self._page.wait_for_timeout(250)

    def _completed_response_text(self, response: str, receipt_marker: str) -> str:
        """Read a fenced vote only while its current-turn attribution remains proven."""
        if not callable(getattr(self._page, "evaluate", None)):
            return normalize_jury_response(response, self.platform)
        try:
            snapshot = web_agent._provider_turn_snapshot(
                self._page,
                self.platform,
                receipt_marker=receipt_marker,
                response_object_key="verdict",
            )
        except Exception:
            return normalize_jury_response(response, self.platform)
        if (
            snapshot.get("structuredResponse")
            and snapshot.get("markerEchoed")
            and snapshot.get("assistantAfterLatestUser")
            and not snapshot.get("generating")
            and web_agent._web_target_is_open(
                self.platform, self._conversation_url, str(snapshot.get("url") or ""),
            )
        ):
            return str(snapshot.get("text") or response)
        return normalize_jury_response(response, self.platform)

    def __enter__(self) -> JuryBrowserSession:
        if self._entered:
            raise RuntimeError("A jury browser session cannot be opened twice.")
        self._entered = True
        self._owner_thread = get_ident()
        self._require_running()
        resources = ExitStack()
        self._resources = resources
        try:
            descriptor = browser_descriptors(self.config)[self.settings.browser]
            playwright = resources.enter_context(sync_playwright_or_error())
            self._require_running()
            context = resources.enter_context(launch_chromium_context(
                playwright,
                descriptor,
                headless=False,
                clone_profile_first=True,
                background_window=True,
                silent=True,
                window_mode=(
                    CHROMIUM_WINDOW_MODE_TASK_STAGE
                    if is_macos_host()
                    else CHROMIUM_WINDOW_MODE_OFFSCREEN
                ),
                allow_cdp_attach=False,
            ))
            self._require_running()
            self._page = context.new_page()
            goto_with_retry(
                self._page,
                self.settings.target_url,
                attempts=2,
                timeout_ms=90_000,
                should_stop=self.stop_event.is_set,
            )
            self._require_running()
            self._binding = web_agent._ProviderSessionBinding(
                self._page, self.platform, self.settings.target_url, "new",
            )
            if not web_agent._verify_agent_page(
                self._page,
                "chromium",
                self.platform,
                self.settings.target_url,
                self.stop_event.is_set,
                self._availability_check,
            ):
                self._require_running()
                raise RuntimeError("The juror's authenticated browser composer is unavailable.")
            self._binding.check()
            if not self._binding.prepare_fresh_session(self.stop_event.is_set):
                self._require_running()
                raise RuntimeError("The juror's fresh conversation could not be verified.")
            self._require_running()
            if self.platform == "chatgpt":
                web_agent._select_chat_mode(self._page, "chromium")
            selected = False
            for attempt in range(2):
                self.model_observation.clear()
                selected = web_agent._select_web_model(
                    self._page,
                    "chromium",
                    self.platform,
                    self.settings.model,
                    self.model_observation,
                    should_stop=self.stop_event.is_set,
                    availability_check=self._availability_check,
                    chatgpt_effort=self.settings.chatgpt_effort,
                    session_type=web_agent.session_type_for_mode("new"),
                    model_option=self.model_option,
                )
                self.model_observation_history.append(dict(self.model_observation))
                if (
                    selected
                    or self.platform != "chatgpt"
                    or self.model_observation.get("reason") not in {
                        "requested-effort-control-not-found", "power-control-recycled",
                    }
                    or attempt == 1
                ):
                    break
                # A fresh page can hydrate its effort control after the model
                # menu. Rebind that same page once before admitting any prompt.
                self._availability_check()
                self._binding.check()
                self._page.wait_for_timeout(500)
            self._require_running()
            self._binding.check()
            if self.platform == "chatgpt":
                selected = bool(
                    selected
                    and self.model_observation.get("effort_catalog_complete")
                    and str(self.model_observation.get("thinking_effort") or "").casefold()
                    == self.settings.chatgpt_effort.casefold()
                )
            if not selected:
                reason = self.model_observation.get("reason") or "model-readback-mismatch"
                raise RuntimeError(
                    f"{self.platform} could not verify "
                    f"{self.model_option['display_label']}: {reason}. "
                    "No jury prompt was sent."
                )
            return self
        except BaseException:
            self._failed = True
            resources.close()
            self._resources = None
            raise

    def ask(self, prompt: str, *, timeout_seconds: float | None = None) -> str:
        """Submit once and read the current juror response in its bound session."""
        self._require_owner()
        self._require_running()
        if self._resources is None or self._failed:
            raise RuntimeError("This jury browser session is closed or requires review.")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("A juror prompt is required.")
        try:
            self._availability_check()
            self._binding.check()
            message = prompt
            if self._turn_count == 0:
                message = self._binding.arm_first_submission(message)
            marker = (
                self._binding.submission_marker
                if self.platform == "chatgpt" and self._turn_count == 0
                else f"agent-turn-{secrets.token_hex(16)}"
            )
            response = web_agent._submit_and_wait(
                self._page,
                "chromium",
                message,
                self.stop_event.is_set,
                platform=self.platform,
                session_check=self._binding.check,
                session_recover=lambda should_stop: self._recover_response_session(
                    marker, should_stop,
                ),
                submission_target_url=self._conversation_url or self.settings.target_url,
                session_mode="new" if self._turn_count == 0 else "recent",
                availability_check=self._availability_check,
                turn_receipt_marker=marker,
                on_response_state=self._record_response_state,
                timeout_seconds=timeout_seconds,
            )
            self._require_running()
            if not response.strip():
                raise RuntimeError("The juror returned no complete response.")
            self.last_raw_response = response
            self._conversation_url = self._binding.require_created_conversation(
                self.stop_event.is_set,
            )
            self._require_running()
            if not self._conversation_url:
                raise RuntimeError("The juror's conversation could not be verified.")
            self._turn_count += 1
            if self.on_conversation is not None:
                self.on_conversation(self._conversation_url)
            return self._completed_response_text(response, marker)
        except BaseException:
            self._failed = True
            self._conversation_url = (
                self._binding.bound_conversation_url or self._conversation_url
            )
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self._require_owner()
        if self._resources is not None:
            resources, self._resources = self._resources, None
            return bool(resources.__exit__(exc_type, exc, traceback))
        return False
