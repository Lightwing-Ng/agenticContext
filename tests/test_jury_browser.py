"""Verify isolated browser ownership and single-session jury exchanges.

Code version: v1.2.0-codex.1
"""

from contextlib import contextmanager
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from app.core import jury_browser as jury
from app.core.computer_use_agent import ComputerUseSettings


@pytest.fixture
def transport(monkeypatch):
    events = []
    page = SimpleNamespace(
        url="about:blank",
        goto=lambda *_args, **_kwargs: events.append("safari-goto"),
        wait_for_timeout=lambda _milliseconds: events.append("wait"),
    )
    context = SimpleNamespace(new_page=lambda: events.append("new_page") or page)

    @contextmanager
    def resource(label, value):
        events.append(f"open:{label}")
        try:
            yield value
        finally:
            events.append(f"close:{label}")

    def launch(_playwright, _descriptor, **kwargs):
        assert kwargs["clone_profile_first"] is True
        assert kwargs["allow_cdp_attach"] is False
        return resource("context", context)

    class Binding:
        bound_conversation_url = ""
        submission_marker = ""

        def __init__(self, selected_page, platform, target_url, session_mode):
            assert selected_page is page
            assert session_mode == "new"
            self.platform = platform
            events.append("binding")

        def check(self, _transition=False):
            return self.bound_conversation_url

        def prepare_fresh_session(self, _stop):
            events.append("prepare")
            return True

        def arm_first_submission(self, message):
            events.append("arm")
            self.submission_marker = "agent-transfer-" + "a" * 32
            return f"Controller transfer ID: {self.submission_marker}\n\n{message}"

        def ensure_response_session(self, _stop=None):
            return self.bound_conversation_url

        def require_created_conversation(self, _stop):
            self.bound_conversation_url = f"https://{self.platform}.com/c/same-question"
            return self.bound_conversation_url

    selected_models = []
    submissions = []
    browser_kinds = []

    def select(_page, kind, platform, model, observation, **kwargs):
        browser_kinds.append(("select", kind))
        selected_models.append((platform, model, kwargs))
        observation.update(
            observed="Latest" if platform == "chatgpt" else kwargs["model_option"]["label"],
            thinking_effort=kwargs["chatgpt_effort"],
            effort_catalog_complete=True,
        )
        return True

    def submit(selected_page, kind, message, _stop, **kwargs):
        assert selected_page is page
        browser_kinds.append(("submit", kind))
        submissions.append((message, kwargs))
        return f"Verified response {len(submissions)}"

    monkeypatch.setattr(jury, "browser_descriptors", lambda _config: {"edge": object()})
    monkeypatch.setattr(jury, "sync_playwright_or_error", lambda: resource("playwright", object()))
    monkeypatch.setattr(jury, "launch_chromium_context", launch)
    monkeypatch.setattr(jury, "goto_with_retry", lambda *_args, **_kwargs: events.append("goto"))
    monkeypatch.setattr(jury.web_agent, "_ProviderSessionBinding", Binding)
    monkeypatch.setattr(jury.web_agent, "_verify_agent_page", lambda *_args: True)
    monkeypatch.setattr(jury.web_agent, "_select_chat_mode", lambda *_args: events.append("chat_mode"))
    monkeypatch.setattr(jury.web_agent, "_select_web_model", select)
    monkeypatch.setattr(jury.web_agent, "_submit_and_wait", submit)
    monkeypatch.setattr(jury.web_agent, "_provider_human_verification_reason", lambda *_args: "")
    monkeypatch.setattr(
        jury.web_agent,
        "WorkspaceController",
        lambda *_args, **_kwargs: pytest.fail("Jury must not create a workspace controller."),
    )
    monkeypatch.setattr(
        jury.web_agent,
        "_attach_context_file",
        lambda *_args, **_kwargs: pytest.fail("Jury must not upload workspace context."),
    )
    return SimpleNamespace(
        events=events,
        page=page,
        selected_models=selected_models,
        submissions=submissions,
        browser_kinds=browser_kinds,
    )


@pytest.mark.parametrize("platform", ["chatgpt", "grok", "gemini", "claude"])
def test_rounds_reuse_one_page_and_bound_conversation(transport, platform):
    with jury.JuryBrowserSession(ComputerUseSettings(), platform) as session:
        assert session.ask(
            "Fact-check the claim.", timeout_seconds=321,
        ) == "Verified response 1"
        url = session.conversation_url
        assert session.ask("Review the other jurors' evidence.") == "Verified response 2"
        assert session.conversation_url == url
    assert transport.events.count("new_page") == 1
    assert transport.events.count("binding") == 1
    assert transport.events.count("goto") == 1
    assert transport.events.count("arm") == 1
    assert transport.events[-2:] == ["close:context", "close:playwright"]
    first, second = transport.submissions
    assert first[1]["session_mode"] == "new"
    assert first[1]["timeout_seconds"] == 321
    assert second[1]["session_mode"] == "recent"
    assert second[1]["timeout_seconds"] is None
    assert second[1]["submission_target_url"] == url
    assert first[1]["turn_receipt_marker"] != second[1]["turn_receipt_marker"]


@pytest.mark.parametrize("platform,selection,model,label,effort", [
    ("chatgpt", "chatgpt-latest-extra-high", "live:latest", "Latest", "Extra High"),
    ("chatgpt", "chatgpt-latest-high", "live:latest", "Latest", "High"),
    ("grok", "grok-auto", "grok-auto", "Auto", "Extra High"),
    ("grok", "grok-build", "grok-build", "Build", "Extra High"),
    ("gemini", "gemini-3.1-pro", "gemini-3.1-pro", "Gemini 3.1 Pro", "Extra High"),
    ("gemini", "gemini-3.8-flash", "gemini-3.8-flash", "Gemini 3.8 Flash", "Extra High"),
])
def test_requested_model_does_not_inherit_agent_defaults(
    transport, platform, selection, model, label, effort,
):
    original = ComputerUseSettings(model="grok-build", chatgpt_effort="highest_available")
    with jury.JuryBrowserSession(original, platform, model_selection=selection):
        pass
    selected_platform, selected_model, kwargs = transport.selected_models[0]
    assert (selected_platform, selected_model) == (platform, model)
    assert kwargs["model_option"]["label"] == label
    assert kwargs["chatgpt_effort"] == effort
    assert original.model == "grok-build"
    assert original.chatgpt_effort == "highest_available"


def test_unknown_jury_model_tier_is_rejected_before_browser_launch(transport):
    with pytest.raises(ValueError, match="supported model tier"):
        jury.JuryBrowserSession(
            ComputerUseSettings(), "gemini", model_selection="gemini-unknown",
        )
    assert transport.events == []


def test_gemini_waits_for_the_accepted_first_turn_url_without_another_submission(transport, monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(jury.time, "monotonic", lambda: elapsed[0])
    receipts = []
    monkeypatch.setattr(
        jury.web_agent, "_provider_turn_snapshot",
        lambda *_args, **kwargs: receipts.append(kwargs["receipt_marker"]) or {"markerEchoed": True},
    )
    reported = []
    with jury.JuryBrowserSession(ComputerUseSettings(), "gemini", on_conversation=reported.append) as session:
        session._page.wait_for_timeout = lambda milliseconds: elapsed.__setitem__(0, elapsed[0] + milliseconds / 1000)

        def bind(_stop):
            if elapsed[0] >= 8:
                session._binding.bound_conversation_url = "https://gemini.google.com/app/one-question"
            return session._binding.bound_conversation_url

        session._binding.ensure_response_session = bind
        assert session._recover_response_session("current-receipt") == "https://gemini.google.com/app/one-question"
    assert elapsed[0] == 8
    assert set(receipts) == {"current-receipt"}
    assert reported == ["https://gemini.google.com/app/one-question"]
    assert transport.submissions == []
    assert transport.events.count("new_page") == 1


@pytest.mark.parametrize("received", [False, True])
def test_gemini_first_turn_wait_requires_its_receipt_and_remains_bounded(transport, monkeypatch, received):
    elapsed = [0.0]
    monkeypatch.setattr(jury.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(jury.web_agent, "_provider_turn_snapshot", lambda *_args, **_kwargs: {"markerEchoed": received})
    with jury.JuryBrowserSession(ComputerUseSettings(), "gemini") as session:
        session._page.wait_for_timeout = lambda milliseconds: elapsed.__setitem__(0, elapsed[0] + milliseconds / 1000)
        if received:
            with pytest.raises(RuntimeError, match="message was not resent"):
                session._recover_response_session("current-receipt")
            assert elapsed[0] == 60
        else:
            assert session._recover_response_session("current-receipt") == ""
            assert elapsed[0] == 0
    assert transport.submissions == []


def test_stop_cancels_gemini_first_turn_url_wait(transport, monkeypatch):
    stop = Event()
    monkeypatch.setattr(jury.web_agent, "_provider_turn_snapshot", lambda *_args, **_kwargs: {"markerEchoed": True})
    with jury.JuryBrowserSession(ComputerUseSettings(), "gemini", stop) as session:
        session._page.wait_for_timeout = lambda _milliseconds: stop.set()
        with pytest.raises(jury.JuryBrowserStopped):
            session._recover_response_session("current-receipt")
    assert transport.submissions == []


@pytest.mark.parametrize("missing_proof", ["effort_catalog_complete", "thinking_effort"])
def test_chatgpt_effort_is_fail_closed_before_any_submission(transport, monkeypatch, missing_proof):
    def select(*args, **kwargs):
        args[4].update(thinking_effort="Extra High", effort_catalog_complete=True)
        args[4].pop(missing_proof)
        return True

    monkeypatch.setattr(jury.web_agent, "_select_web_model", select)
    with pytest.raises(RuntimeError, match="No jury prompt was sent"):
        with jury.JuryBrowserSession(ComputerUseSettings(), "chatgpt"):
            pytest.fail("Unverified effort must never admit a prompt.")
    assert transport.submissions == []
    assert transport.events[-2:] == ["close:context", "close:playwright"]


def test_transient_effort_control_rebinds_only_the_same_page_before_submission(transport, monkeypatch):
    calls = []

    def select(page, _kind, _platform, _model, observation, **_kwargs):
        calls.append(page)
        if len(calls) == 1:
            observation.update(reason="requested-effort-control-not-found", observed="Latest")
            return False
        observation.update(thinking_effort="Extra High", effort_catalog_complete=True, observed="Latest")
        return True

    monkeypatch.setattr(jury.web_agent, "_select_web_model", select)
    with jury.JuryBrowserSession(ComputerUseSettings(), "chatgpt") as session:
        assert len(session.model_observation_history) == 2
        assert session.model_observation_history[0]["reason"] == "requested-effort-control-not-found"
        assert transport.submissions == []
        session.ask("Claim")
    assert len(calls) == 2
    assert calls[0] is calls[1]
    assert transport.events.count("new_page") == 1
    assert transport.events.count("goto") == 1


@pytest.mark.parametrize("reason,attempts", [
    ("requested-effort-control-not-found", 2),
    ("requested-effort-unavailable", 1),
    ("effort-selection-label-mismatch", 1),
])
def test_permanent_model_failure_remains_fail_closed(transport, monkeypatch, reason, attempts):
    calls = []

    def select(_page, _kind, _platform, _model, observation, **_kwargs):
        calls.append("select")
        observation.update(reason=reason)
        return False

    monkeypatch.setattr(jury.web_agent, "_select_web_model", select)
    session = jury.JuryBrowserSession(ComputerUseSettings(), "chatgpt")
    with pytest.raises(RuntimeError, match="No jury prompt was sent"):
        with session:
            pass
    assert len(calls) == attempts
    assert len(session.model_observation_history) == attempts
    assert transport.submissions == []
    assert transport.events.count("new_page") == 1
    assert transport.events.count("goto") == 1


def test_ambiguous_submission_is_never_retried_or_reopened(transport, monkeypatch):
    calls = []

    def ambiguous(*_args, **_kwargs):
        calls.append("submit")
        raise TimeoutError("Delivery could not be proved.")

    monkeypatch.setattr(jury.web_agent, "_submit_and_wait", ambiguous)
    with jury.JuryBrowserSession(ComputerUseSettings(), "grok") as session:
        with pytest.raises(TimeoutError):
            session.ask("Claim")
        with pytest.raises(RuntimeError, match="closed or requires review"):
            session.ask("Retry claim")
    assert calls == ["submit"]
    assert transport.events.count("new_page") == 1


def test_bound_conversation_is_reported_before_a_response_timeout(transport, monkeypatch):
    reported = []
    url = "https://grok.com/c/same-question"

    def ambiguous(*_args, **kwargs):
        binding = kwargs["session_check"].__self__
        binding.bound_conversation_url = url
        kwargs["on_response_state"](conversation_url=url, generating=True)
        assert reported == [url]
        raise TimeoutError("The submitted turn is still generating.")

    monkeypatch.setattr(jury.web_agent, "_submit_and_wait", ambiguous)
    with jury.JuryBrowserSession(
        ComputerUseSettings(), "grok", on_conversation=reported.append,
    ) as session:
        with pytest.raises(TimeoutError):
            session.ask("Claim")
        assert session.conversation_url == url
    assert session.conversation_url == url
    assert reported == [url]


def test_stopped_partial_response_is_not_accepted(transport, monkeypatch):
    stop_event = Event()

    def canceled(*_args, **_kwargs):
        stop_event.set()
        return "Incomplete provisional text"

    monkeypatch.setattr(jury.web_agent, "_submit_and_wait", canceled)
    with jury.JuryBrowserSession(ComputerUseSettings(), "grok", stop_event) as session:
        with pytest.raises(jury.JuryBrowserStopped):
            session.ask("Claim")
    assert transport.events[-2:] == ["close:context", "close:playwright"]


def test_preexisting_stop_opens_no_browser(transport):
    stop_event = Event()
    stop_event.set()
    with pytest.raises(jury.JuryBrowserStopped):
        with jury.JuryBrowserSession(ComputerUseSettings(), "grok", stop_event):
            pass
    assert transport.events == []


def test_cross_thread_prompt_is_rejected_before_browser_io(transport):
    failures = []
    with jury.JuryBrowserSession(ComputerUseSettings(), "grok") as session:
        def wrong_thread():
            try:
                session.ask("Claim")
            except RuntimeError as error:
                failures.append(str(error))

        thread = Thread(target=wrong_thread)
        thread.start()
        thread.join()
    assert failures == ["A jury browser session must stay on its owning execution thread."]
    assert transport.submissions == []


@pytest.mark.parametrize("platform", ["chatgpt", "grok", "gemini", "claude"])
def test_login_check_verifies_each_chat_without_submission(transport, platform):
    result = jury.jury_browser_login_check(ComputerUseSettings(), platform)
    assert result["ready"] is result["logged_in"] is True
    assert result["can_download"] is False
    assert transport.submissions == []
    assert transport.events[-2:] == ["close:context", "close:playwright"]


def test_login_check_verifies_the_selected_model_tier(transport):
    result = jury.jury_browser_login_check(
        ComputerUseSettings(),
        "gemini",
        model_selection="gemini-3.8-flash",
    )
    assert result["ready"] is True
    assert transport.selected_models[0][1] == "gemini-3.8-flash"


def test_safari_login_check_uses_an_owned_safari_page_without_edge_fallback(
    transport,
    monkeypatch,
):
    class SafariContext:
        def __init__(self, initial_url, *, lock_blocking):
            assert initial_url == "https://grok.com/"
            assert lock_blocking is False
            self.primary_page = transport.page

        def __enter__(self):
            transport.events.append("open:safari-context")
            return self

        def __exit__(self, exc_type, exc, traceback):
            transport.events.append("close:safari-context")

    monkeypatch.setattr(jury, "is_macos_host", lambda: True)
    monkeypatch.setattr(jury, "SafariContext", SafariContext)
    result = jury.jury_browser_login_check(
        ComputerUseSettings(browser="safari"),
        "grok",
    )

    assert result["ready"] is result["logged_in"] is True
    assert transport.events == [
        "open:safari-context",
        "safari-goto",
        "binding",
        "prepare",
        "close:safari-context",
    ]
    assert transport.browser_kinds == [("select", "safari")]
    assert transport.submissions == []


def test_human_verification_surfaces_and_retains_the_same_page(transport, monkeypatch):
    reason = "Human verification required: Grok requires security challenge control."
    recoveries = []

    monkeypatch.setattr(
        jury.web_agent,
        "_provider_human_verification_reason",
        lambda *_args: reason,
    )

    def wait_for_recovery(**kwargs):
        recoveries.append(kwargs)
        assert transport.events[-1] != "close:context"
        return "recovered"

    monkeypatch.setattr(jury.web_agent, "_wait_for_browser_recovery", wait_for_recovery)
    with jury.JuryBrowserSession(ComputerUseSettings(), "grok") as session:
        page = session._page
        assert session._availability_check() is True
        assert session._page is page
        assert "close:context" not in transport.events

    assert len(recoveries) == 1
    recovery = recoveries[0]
    assert recovery["page"] is page
    assert recovery["reason"] == reason
    assert recovery["should_resume"] is None
    assert recovery["monitor_screen_lock"] is False
    assert transport.events[-2:] == ["close:context", "close:playwright"]


def test_human_verification_stop_closes_without_touching_the_challenge(transport, monkeypatch):
    reason = "Human verification required: Grok requires security challenge control."
    monkeypatch.setattr(
        jury.web_agent,
        "_provider_human_verification_reason",
        lambda *_args: reason,
    )
    monkeypatch.setattr(
        jury.web_agent,
        "_wait_for_browser_recovery",
        lambda **_kwargs: "stopped",
    )
    with jury.JuryBrowserSession(ComputerUseSettings(), "grok") as session:
        with pytest.raises(jury.JuryBrowserStopped, match="human verification"):
            session._availability_check()
    assert transport.submissions == []
    assert transport.events[-2:] == ["close:context", "close:playwright"]


def test_grok_login_check_does_not_accept_an_unverified_composer(transport, monkeypatch):
    monkeypatch.setattr(jury.web_agent, "_verify_agent_page", lambda *_args: False)
    with pytest.raises(RuntimeError, match="authenticated browser composer"):
        jury.jury_browser_login_check(ComputerUseSettings(), "grok")
    assert transport.submissions == []
    assert transport.events[-2:] == ["close:context", "close:playwright"]


def test_unsupported_browser_is_rejected_without_launch(transport):
    with pytest.raises(ValueError, match="Safari, Microsoft Edge, or Google Chrome"):
        jury.JuryBrowserSession(ComputerUseSettings(browser="firefox"), "chatgpt")
    assert transport.events == []


@pytest.mark.parametrize("platform", ["gemini", "claude"])
def test_safari_source_only_providers_are_rejected_before_browser_activity(
    transport,
    platform,
    monkeypatch,
):
    monkeypatch.setattr(jury, "is_macos_host", lambda: True)
    with pytest.raises(ValueError, match="Safari Jury supports ChatGPT and Grok"):
        jury.JuryBrowserSession(ComputerUseSettings(browser="safari"), platform)
    assert transport.events == []
    assert transport.submissions == []


@pytest.mark.parametrize("platform", ["chatgpt", "grok"])
def test_safari_juror_uses_the_injected_owned_page_without_chromium_fallback(
    transport,
    platform,
    monkeypatch,
):
    monkeypatch.setattr(jury, "is_macos_host", lambda: True)
    with jury.JuryBrowserSession(
        ComputerUseSettings(browser="safari"),
        platform,
        browser_page=transport.page,
    ) as session:
        assert session.ask("Fact-check the claim.") == "Verified response 1"

    assert transport.events.count("safari-goto") == 1
    assert not any(event.startswith("open:") for event in transport.events)
    assert not any(event == "new_page" for event in transport.events)
    assert transport.browser_kinds[-2:] == [("select", "safari"), ("submit", "safari")]


def test_explicit_grok_model_uses_existing_trusted_selector_without_changing_agent_catalog(monkeypatch):
    calls = []
    existing = jury.web_agent._platform_model_options("grok")
    page = SimpleNamespace(locator=lambda _selector: None)

    def select(_page, browser_kind, labels, triggers, _observation, _stop):
        calls.append((browser_kind, labels, triggers))
        return True

    monkeypatch.setattr(jury.web_agent, "_select_grok_model_with_trusted_clicks", select)
    assert jury.web_agent._select_web_model(
        page, "chromium", "grok", "grok-auto",
        model_option=jury.JURY_MODEL_OPTIONS["grok"],
    )
    assert calls == [("chromium", ("Auto",), ("Auto",))]
    assert jury.web_agent._platform_model_options("grok") == existing


def test_explicit_gemini_model_passes_exact_label_into_existing_menu_verifier():
    calls = []
    existing = jury.web_agent._platform_model_options("gemini")

    def evaluate(_script, arguments):
        calls.append(arguments)
        return {"ok": True, "selected": "3.1 Pro", "available": ["3.1 Pro", "3.8 Flash"]}

    page = SimpleNamespace(evaluate=evaluate)
    assert jury.web_agent._select_web_model(
        page, "chromium", "gemini", "gemini-3.1-pro",
        model_option=jury.JURY_MODEL_OPTIONS["gemini"],
    )
    assert calls[0]["uiLabel"] == "3.1 Pro"
    assert calls[0]["remoteLabels"] == ["Gemini 3.1 Pro", "3.1 Pro"]
    assert jury.web_agent._platform_model_options("gemini") == existing


def test_grok_timing_and_source_count_envelope_preserves_the_exact_json_vote():
    body = '{"verdict":"misleading","conclusion":"A claim needs evidence."}'
    response = f"Worked for 14s\n\n{body}\n\n45 sources"
    assert jury.normalize_jury_response(response, "grok") == body
    assert jury.normalize_jury_response(response, "chatgpt") == response


@pytest.mark.parametrize("body", [
    '{"verdict":"misleading"} followed by an objection',
    '{"verdict":"misleading"}\n{"verdict":"supported"}',
    '[{"verdict":"misleading"}]',
    '{"conclusion":"invalid\\escape"}',
])
def test_grok_envelope_does_not_repair_or_truncate_invalid_model_output(body):
    response = f"Worked for 18s\n\n{body}\n\n15 sources"
    assert jury.normalize_jury_response(response, "grok") == response


def test_unknown_provider_prose_is_never_discarded_as_a_grok_envelope():
    response = 'My preliminary view:\n\n{"verdict":"misleading"}\n\n45 sources'
    assert jury.normalize_jury_response(response, "grok") == response


def test_original_provider_envelope_remains_available_for_audit(transport, monkeypatch):
    body = '{"verdict":"misleading","conclusion":"Check the evidence."}'
    response = f"Worked for 14s\n\n{body}\n\n45 sources"
    monkeypatch.setattr(jury.web_agent, "_submit_and_wait", lambda *_args, **_kwargs: response)
    with jury.JuryBrowserSession(ComputerUseSettings(), "grok") as session:
        assert session.ask("Claim") == body
        assert session.last_raw_response == response


def test_grok_fenced_vote_envelope_requires_an_entire_valid_json_object():
    body = '{"verdict":"misleading","conclusion":"Use source evidence."}'
    response = f"Worked for 18s\n\n```json\n{body}\n```\n\n15 sources"
    assert jury.normalize_jury_response(response, "grok") == body


@pytest.mark.parametrize("missing_proof", [None, "markerEchoed", "assistantAfterLatestUser", "structuredResponse", "url", "generating"])
def test_fenced_body_requires_current_receipt_order_session_and_completion(monkeypatch, missing_proof):
    session = jury.JuryBrowserSession(ComputerUseSettings(), "grok")
    session._page = SimpleNamespace(evaluate=lambda *_args: None)
    session._conversation_url = "https://grok.com/c/same-question"
    body = '{"verdict":"misleading","conclusion":"Use source evidence."}'
    raw = f"Worked for 18s\n\njson Copy code\n{body}\n\n15 sources"
    snapshot = {
        "structuredResponse": True, "markerEchoed": True,
        "assistantAfterLatestUser": True, "generating": False,
        "url": session._conversation_url, "text": body,
    }
    if missing_proof == "url":
        snapshot["url"] = "https://grok.com/c/different-question"
    elif missing_proof == "generating":
        snapshot["generating"] = True
    elif missing_proof:
        snapshot[missing_proof] = False

    def read(_page, _platform, **kwargs):
        assert kwargs == {"receipt_marker": "current-turn", "response_object_key": "verdict"}
        return snapshot

    monkeypatch.setattr(jury.web_agent, "_provider_turn_snapshot", read)
    assert session._completed_response_text(raw, "current-turn") == (raw if missing_proof else body)
