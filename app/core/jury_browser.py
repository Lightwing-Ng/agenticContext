"""Keep each juror in one authenticated browser conversation for an entire question.

Code version: v1.8.0-codex.2
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
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
from .safari_automation import SafariContext


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
            "accept_current": True,
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
    from .jury import SAFARI_JURY_PROVIDER_ERROR, SAFARI_JURY_PROVIDERS

    if platform not in JURY_MODEL_OPTIONS:
        raise ValueError(f"Unsupported jury provider: {platform}")
    if settings.browser == "safari":
        if not is_macos_host():
            raise ValueError("Safari Jury sessions require macOS.")
        if platform not in SAFARI_JURY_PROVIDERS:
            raise ValueError(SAFARI_JURY_PROVIDER_ERROR)
        return
    if settings.browser not in {"edge", "chrome"}:
        raise ValueError("Choose Safari, Microsoft Edge, or Google Chrome for Jury.")


def _login_check_result(
    settings: ComputerUseSettings,
    platform: str,
    *,
    logged_in: bool,
    message: str,
) -> dict[str, Any]:
    return {
        "platform": platform,
        "browser": settings.browser,
        "logged_in": logged_in,
        "ready": logged_in,
        "can_download": False,
        "message": message,
    }


def jury_browser_login_check(
    settings: ComputerUseSettings,
    platform: str,
    *,
    config: CrawlConfig | None = None,
    model_selection: str | None = None,
    browser_page: Any = None,
    stop_event: Event | None = None,
    project_profile_root: Path | None = None,
) -> dict[str, Any]:
    """Check the selected provider in its headed Jury browser without a prompt."""
    _validate_provider(settings, platform)
    shutdown = stop_event or Event()
    if shutdown.is_set():
        raise JuryBrowserStopped("Jury shutdown canceled the account check.")
    with JuryBrowserSession(
        settings,
        platform,
        shutdown,
        config=config,
        model_selection=model_selection,
        browser_page=browser_page,
        project_profile_root=project_profile_root,
    ):
        return _login_check_result(
            settings,
            platform,
            logged_in=True,
            message=f"{platform} chat is signed in and ready.",
        )


def jury_safari_account_check(
    settings: ComputerUseSettings,
    providers: list[str],
    selections: dict[str, dict[str, Any]],
    *,
    config: CrawlConfig | None = None,
    stop_event: Event | None = None,
) -> list[dict[str, Any]]:
    """Check every selected Safari juror in one task-owned window without a prompt."""
    for platform in providers:
        _validate_provider(settings, platform)
    shutdown = stop_event or Event()
    outcomes: dict[str, dict[str, Any] | BaseException] = {}
    context = SafariContext("about:blank", lock_blocking=False)
    entered = False
    try:
        if shutdown.is_set():
            raise JuryBrowserStopped("Jury shutdown canceled the account check.")
        context.__enter__()
        entered = True
        pages = [context.primary_page]
        try:
            for _index in range(1, len(providers)):
                if shutdown.is_set():
                    raise JuryBrowserStopped(
                        "Jury shutdown canceled the account check."
                    )
                pages.append(context.new_page())
        except Exception as exc:
            for platform in providers[len(pages):]:
                outcomes[platform] = exc
        for platform, page in zip(providers, pages):
            try:
                if shutdown.is_set():
                    raise JuryBrowserStopped(
                        "Jury shutdown canceled the account check."
                    )
                outcomes[platform] = jury_browser_login_check(
                    settings,
                    platform,
                    config=config,
                    model_selection=selections[platform]["selection_key"],
                    browser_page=page,
                    stop_event=shutdown,
                )
            except Exception as exc:
                outcomes[platform] = exc
    except Exception as exc:
        for platform in providers:
            outcomes.setdefault(platform, exc)
    finally:
        if entered:
            try:
                context.__exit__(None, None, None)
            except Exception as exc:
                for platform in providers:
                    outcomes[platform] = RuntimeError(
                        "Safari could not close its task-owned account-check "
                        f"window: {exc}"
                    )
    results = []
    for platform in providers:
        outcome = outcomes.get(platform)
        if isinstance(outcome, dict):
            results.append(outcome)
            continue
        results.append(
            _login_check_result(
                settings,
                platform,
                logged_in=False,
                message=(
                    str(outcome)
                    if outcome is not None
                    else "Sign-in could not be verified."
                ),
            )
        )
    return results


def _macos_edge_profile_setup_message(platform: str) -> str:
    """Describe the one-time, non-daily Edge sign-in step for one provider."""
    return (
        "The project-owned Edge Jury profile was opened for one-time setup. "
        f"Sign in to {platform} in that Edge window, then choose Check accounts again. "
        "The daily Edge profile was not copied or opened."
    )


def _open_macos_edge_profile_setup_tabs(
    providers: list[str],
    *,
    project_profile_root: Path | None,
) -> dict[str, BaseException | None]:
    """Open one setup tab per provider in the persistent project Edge profile."""
    from .agent_debug_browser import debug_browser_lock, ensure_debug_browser

    outcomes: dict[str, BaseException | None] = {platform: None for platform in providers}
    with debug_browser_lock("edge"):
        handle = ensure_debug_browser("edge", profile_root=project_profile_root)
        with sync_playwright_or_error() as playwright:
            browser = playwright.chromium.connect_over_cdp(handle.cdp_endpoint)
            try:
                if not browser.contexts:
                    raise RuntimeError(
                        "The project Edge profile has no persistent browser context."
                    )
                context = browser.contexts[0]
                first_page = None
                for platform in providers:
                    try:
                        page = context.new_page()
                        if first_page is None:
                            first_page = page
                        goto_with_retry(
                            page,
                            web_agent._platform_home_url(platform),
                            attempts=2,
                            timeout_ms=90_000,
                        )
                    except Exception as exc:
                        outcomes[platform] = exc
                bring_to_front = getattr(first_page, "bring_to_front", None)
                if callable(bring_to_front):
                    bring_to_front()
            finally:
                browser.close()
    return outcomes


def jury_macos_edge_account_check(
    settings: ComputerUseSettings,
    providers: list[str],
    selections: dict[str, dict[str, Any]],
    *,
    config: CrawlConfig | None = None,
    stop_event: Event | None = None,
    project_profile_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Check macOS jurors through one persistent project Edge process."""
    if not is_macos_host() or settings.browser != "edge":
        raise RuntimeError("The project Edge account check requires macOS Edge Jury.")
    for platform in providers:
        _validate_provider(settings, platform)
    shutdown = stop_event or Event()
    if shutdown.is_set():
        raise JuryBrowserStopped("Jury shutdown canceled the account check.")

    from .agent_debug_browser import debug_browser_profile_initialized

    if not debug_browser_profile_initialized(
        "edge",
        profile_root=project_profile_root,
    ):
        try:
            setup_outcomes = _open_macos_edge_profile_setup_tabs(
                providers,
                project_profile_root=project_profile_root,
            )
        except Exception as exc:
            setup_outcomes = {platform: exc for platform in providers}
        return [
            _login_check_result(
                settings,
                platform,
                logged_in=False,
                message=(
                    str(setup_outcomes[platform])
                    if setup_outcomes[platform] is not None
                    else _macos_edge_profile_setup_message(platform)
                ),
            )
            for platform in providers
        ]

    results = []
    for platform in providers:
        if shutdown.is_set():
            outcome: dict[str, Any] | BaseException = JuryBrowserStopped(
                "Jury shutdown canceled the account check."
            )
        else:
            try:
                outcome = jury_browser_login_check(
                    settings,
                    platform,
                    config=config,
                    model_selection=selections[platform]["selection_key"],
                    stop_event=shutdown,
                    project_profile_root=project_profile_root,
                )
            except Exception as exc:
                outcome = exc
        if isinstance(outcome, dict):
            results.append(outcome)
        else:
            results.append(
                _login_check_result(
                    settings,
                    platform,
                    logged_in=False,
                    message=str(outcome) or "Sign-in could not be verified.",
                )
            )
    return results


class JuryBrowserSession:
    """Own one browser page on one execution thread, with no workspace controller.

    The coordinator must enter, ask, and exit on the same execution thread. A
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
        browser_page: Any = None,
        project_profile_root: Path | None = None,
    ) -> None:
        _validate_provider(settings, platform)
        if browser_page is not None and settings.browser != "safari":
            raise ValueError("A shared browser page is supported only for Safari Jury sessions.")
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
        self.cleanup_error: BaseException | None = None
        self._resources: ExitStack | None = None
        self._page: Any = None
        self._provided_page = browser_page
        self._project_profile_root = (
            Path(project_profile_root).expanduser()
            if project_profile_root is not None
            else None
        )
        self._browser_kind = "safari" if settings.browser == "safari" else "chromium"
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
            raise RuntimeError("A jury browser session must stay on its owning execution thread.")

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
                monitor_screen_lock=self._browser_kind == "safari",
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
            if self.settings.browser == "safari":
                if self._provided_page is None:
                    context = resources.enter_context(SafariContext(
                        self.settings.target_url,
                        lock_blocking=False,
                    ))
                    self._page = context.primary_page
                else:
                    self._page = self._provided_page
                self._page.goto(
                    self.settings.target_url,
                    wait_until="domcontentloaded",
                    timeout=90_000,
                )
            else:
                descriptor = browser_descriptors(self.config)[self.settings.browser]
                playwright = resources.enter_context(sync_playwright_or_error())
                self._require_running()
                use_project_edge = bool(
                    is_macos_host() and self.settings.browser == "edge"
                )
                context = resources.enter_context(launch_chromium_context(
                    playwright,
                    descriptor,
                    headless=False,
                    clone_profile_first=not use_project_edge,
                    background_window=True,
                    silent=True,
                    window_mode=(
                        CHROMIUM_WINDOW_MODE_TASK_STAGE
                        if is_macos_host()
                        else CHROMIUM_WINDOW_MODE_OFFSCREEN
                    ),
                    allow_cdp_attach=use_project_edge,
                    use_project_debug_profile=use_project_edge,
                    project_profile_root=self._project_profile_root,
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
                self._browser_kind,
                self.platform,
                self.settings.target_url,
                self.stop_event.is_set,
                self._availability_check,
            ):
                self._require_running()
                if is_macos_host() and self.settings.browser == "edge":
                    raise RuntimeError(
                        f"Sign in to {self.platform} in the project-owned Edge Jury "
                        "profile, then choose Check accounts again. The daily Edge "
                        "profile was not used."
                    )
                raise RuntimeError("The juror's authenticated browser composer is unavailable.")
            self._binding.check()
            if not self._binding.prepare_fresh_session(self.stop_event.is_set):
                self._require_running()
                raise RuntimeError("The juror's fresh conversation could not be verified.")
            self._require_running()
            if self.platform == "chatgpt":
                web_agent._select_chat_mode(self._page, self._browser_kind)
            selected = False
            model_attempt_limit = (
                web_agent.CHATGPT_MODEL_CONTROL_RETRY_ATTEMPTS
                if self.platform == "chatgpt"
                else 3
            )
            for attempt in range(model_attempt_limit):
                self.model_observation.clear()
                selected = web_agent._select_web_model(
                    self._page,
                    self._browser_kind,
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
                    or self.model_observation.get("reason")
                    not in web_agent.RETRYABLE_WEB_MODEL_PROOF_REASONS
                    or attempt + 1 >= model_attempt_limit
                ):
                    break
                # A fresh page can hydrate its effort control after the model
                # menu. Rebind that same page once before admitting any prompt.
                self._availability_check()
                self._binding.check()
                self._page.wait_for_timeout(
                    web_agent.CHATGPT_MODEL_CONTROL_RETRY_BACKOFF_MILLISECONDS
                    * (attempt + 1)
                )
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
        except BaseException as primary_error:
            self._failed = True
            try:
                resources.close()
            except BaseException as cleanup_error:
                self.cleanup_error = cleanup_error
                primary_error.add_note(
                    "Jury browser initialization cleanup also failed: "
                    f"{cleanup_error}"
                )
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
            # Safari native focus is only the trusted Send click, not the wait.
            response = web_agent._submit_and_wait(
                self._page,
                self._browser_kind,
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
