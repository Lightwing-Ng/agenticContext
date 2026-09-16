"""Jury deliberation boundaries with deterministic, browser-free jurors.

Code version: v1.8.1-codex.1
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import Future
import json
from threading import Barrier, Event, Lock, Thread, get_ident
import time

import pytest

from app.core import jury as jury_module
from app.core.computer_use_agent import ComputerUseSettings
from app.core.config import CrawlConfig
from app.core.jury import (
    DEFAULT_JURORS,
    SAFARI_JURY_PROVIDERS,
    JuryService,
    build_candidate,
    deliberation_signature,
    parse_opinion,
    unanimous_acceptance,
    validate_model_selections,
    validate_selection,
)


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate(), "The deterministic jury fixture did not settle."


class _ThreadState:
    """Expose a deterministic thread-liveness value for archive cleanup tests."""

    def __init__(self, alive: bool) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


def vote(*, verdict="supported", candidate=None, conclusion="Primary evidence supports the claim.", **changes):
    opinion = {
        "verdict": verdict,
        "conclusion": conclusion,
        "evidence": [{"url": "https://source.example/report", "supports": "The original source states the claim."}],
        "unresolved": [],
        "accept_candidate": candidate is not None,
        "candidate_id": candidate["candidate_id"] if candidate else "",
    }
    opinion.update(changes)
    return opinion


class FakeBrowserFactory:
    """Record each context lifetime, thread identity, and exact shared evidence packet."""

    def __init__(self, answer=None, close_wait=None, close_error=None):
        self.answer = answer or (lambda key, packet, stop: vote(candidate=packet["candidate"]))
        self.close_wait = close_wait
        self.close_error = close_error
        self.lock = Lock()
        self.opened = Counter()
        self.closed = Counter()
        self.threads = defaultdict(list)
        self.packets = defaultdict(list)
        self.model_selections = defaultdict(list)

    def __call__(self, settings, platform, stop, **kwargs):
        owner = self
        owner.model_selections[platform].append(kwargs["model_selection"])

        class Browser:
            conversation_url = f"https://{platform}.example/conversation/one"

            def __enter__(self):
                with owner.lock:
                    owner.opened[platform] += 1
                    owner.threads[platform].append(get_ident())
                return self

            def ask(self, prompt, *, timeout_seconds=None):
                assert timeout_seconds is None or timeout_seconds > 0
                packet = json.loads(prompt.split("Evidence packet (JSON):\n", 1)[1])
                with owner.lock:
                    owner.threads[platform].append(get_ident())
                    owner.packets[platform].append(packet)
                if kwargs.get("on_conversation") is not None:
                    kwargs["on_conversation"](self.conversation_url)
                response = owner.answer(platform, packet, stop)
                return response if isinstance(response, str) else json.dumps(response)

            def __exit__(self, exc_type, exc, traceback):
                if owner.close_wait is not None:
                    assert owner.close_wait.wait(5), "Test did not release its simulated browser shutdown."
                with owner.lock:
                    owner.threads[platform].append(get_ident())
                    owner.closed[platform] += 1
                if owner.close_error is not None:
                    raise RuntimeError(str(owner.close_error))

        return Browser()


@pytest.fixture
def service_factory(tmp_path):
    services = []

    def create(factory=None, login_check=None, root=None):
        service = JuryService(
            lambda: ComputerUseSettings(browser="edge"),
            CrawlConfig,
            root or tmp_path / str(len(services)),
            session_factory=factory or FakeBrowserFactory(),
            login_check=login_check or (lambda *args, **kwargs: {"logged_in": True}),
        )
        services.append(service)
        return service

    yield create
    for service in services:
        service.stop_at_exit()
    for service in services:
        for thread in service.threads.values():
            thread.join(timeout=5)


def complete(service, providers=None, max_rounds=None, models=None):
    initial = service.start(
        "edge", providers or ["chatgpt", "grok"],
        "Check this factual claim.", max_rounds=max_rounds, models=models,
    )
    session_id = initial["session_id"]
    wait_until(lambda: not service.status(session_id)["running"])
    return service.status(session_id)


def test_default_jurors_keep_claude_available_but_unselected():
    assert DEFAULT_JURORS == ("chatgpt", "grok", "gemini")
    assert SAFARI_JURY_PROVIDERS == frozenset(DEFAULT_JURORS)
    assert validate_selection("edge", ["chatgpt", "claude"]) == ("edge", ["chatgpt", "claude"])
    assert validate_selection("chrome", ["chatgpt", "grok", "gemini", "claude"]) == (
        "chrome",
        ["chatgpt", "grok", "gemini", "claude"],
    )
    defaults = validate_model_selections(list(DEFAULT_JURORS), None)
    assert {key: value["selection_key"] for key, value in defaults.items()} == {
        "chatgpt": "chatgpt-latest-extra-high",
        "grok": "grok-auto",
        "gemini": "gemini-3.1-pro",
    }


def test_safari_selection_is_admitted_only_when_the_host_exposes_it(monkeypatch):
    monkeypatch.setattr(
        jury_module,
        "browser_options_for_host",
        lambda: ({"key": "edge"}, {"key": "safari"}),
    )
    assert validate_selection("safari", ["chatgpt", "grok"]) == (
        "safari",
        ["chatgpt", "grok"],
    )
    monkeypatch.setattr(
        jury_module,
        "browser_options_for_host",
        lambda: ({"key": "edge"},),
    )
    with pytest.raises(ValueError, match="supported browser"):
        validate_selection("safari", ["chatgpt", "grok"])


def test_safari_rejects_claude_while_edge_and_chrome_keep_it(monkeypatch):
    monkeypatch.setattr(
        jury_module,
        "browser_options_for_host",
        lambda: ({"key": "edge"}, {"key": "chrome"}, {"key": "safari"}),
    )
    with pytest.raises(ValueError, match="ChatGPT, Grok, and Gemini"):
        validate_selection("safari", ["chatgpt", "claude"])
    with pytest.raises(ValueError, match="ChatGPT, Grok, and Gemini"):
        validate_selection("safari", ["chatgpt", "grok", "gemini", "claude"])
    assert validate_selection("safari", ["chatgpt", "grok", "gemini"]) == (
        "safari",
        ["chatgpt", "grok", "gemini"],
    )
    assert validate_selection("edge", ["chatgpt", "claude"])[1] == ["chatgpt", "claude"]
    assert validate_selection("chrome", ["grok", "claude"])[1] == ["grok", "claude"]


def test_safari_check_and_start_reject_claude_before_browser_activity(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        jury_module,
        "browser_options_for_host",
        lambda: ({"key": "edge"}, {"key": "safari"}),
    )
    probed = []

    def probe(*_args, **_kwargs):
        probed.append("probed")
        return {"logged_in": True}

    service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "safari-claude",
        session_factory=FakeBrowserFactory(),
        login_check=probe,
    )
    with pytest.raises(ValueError, match="ChatGPT, Grok, and Gemini"):
        service.check("safari", ["chatgpt", "claude"])
    with pytest.raises(ValueError, match="ChatGPT, Grok, and Gemini"):
        service.start("safari", ["chatgpt", "claude"], "Do not send this.")
    assert probed == []
    assert service.records == {}


def test_safari_jury_shares_one_context_and_freezes_each_sequential_round(
    tmp_path,
    monkeypatch,
):
    from app.core import jury_browser, safari_automation

    events = []
    owner_threads = []
    fail_grok = [False]

    class SafariContext:
        def __init__(self, initial_url, *, lock_blocking):
            assert initial_url == "about:blank"
            assert lock_blocking is False
            self.primary_page = object()

        def __enter__(self):
            events.append("open:safari-context")
            return self

        def new_page(self):
            events.append("new:safari-page")
            return object()

        def __exit__(self, exc_type, exc, traceback):
            events.append("close:safari-context")

    class SafariSession:
        def __init__(self, settings, platform, stop, **kwargs):
            assert settings.browser == "safari"
            assert kwargs["browser_page"] is not None
            self.platform = platform
            self.stop = stop
            self.on_conversation = kwargs["on_conversation"]
            self.conversation_url = f"https://{platform}.example/conversation/safari"

        def __enter__(self):
            events.append(f"open:{self.platform}")
            owner_threads.append(get_ident())
            return self

        def ask(self, prompt, *, timeout_seconds=None):
            assert timeout_seconds is not None and timeout_seconds > 0
            packet = json.loads(prompt.split("Evidence packet (JSON):\n", 1)[1])
            round_number = int(prompt.split("Round ", 1)[1].split(".", 1)[0])
            events.append(f"ask:{self.platform}:{round_number}")
            owner_threads.append(get_ident())
            self.on_conversation(self.conversation_url)
            if fail_grok[0] and self.platform == "grok":
                raise RuntimeError("Grok failed after ChatGPT returned its vote.")
            return json.dumps(vote(candidate=packet["candidate"]))

        def __exit__(self, exc_type, exc, traceback):
            events.append(f"close:{self.platform}")
            owner_threads.append(get_ident())

    monkeypatch.setattr(
        jury_module,
        "browser_options_for_host",
        lambda: ({"key": "edge"}, {"key": "safari"}),
    )
    monkeypatch.setattr(
        safari_automation,
        "verify_safari_context_cleanup_ready",
        lambda: None,
    )
    monkeypatch.setattr(safari_automation, "SafariContext", SafariContext)
    monkeypatch.setattr(jury_browser, "JuryBrowserSession", SafariSession)
    service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "safari-jury",
        login_check=lambda *_args, **_kwargs: pytest.fail(
            "Safari start must admit the reusable juror sessions directly."
        ),
    )
    try:
        session_id = service.start(
            "safari",
            ["chatgpt", "grok", "gemini"],
            "Check this factual claim.",
        )["session_id"]
        wait_until(lambda: not service.status(session_id)["running"])
        final = service.status(session_id)
    finally:
        service.stop_at_exit()

    assert final["phase"] == "consensus"
    assert events.count("open:safari-context") == 1
    assert events.count("new:safari-page") == 2
    assert {
        (opinion["provider"], opinion["conversation_url"])
        for round_record in final["rounds"]
        for opinion in round_record["opinions"]
    } == {
        ("chatgpt", "https://chatgpt.example/conversation/safari"),
        ("grok", "https://grok.example/conversation/safari"),
        ("gemini", "https://gemini.example/conversation/safari"),
    }
    assert [event for event in events if event.startswith("ask:")] == [
        "ask:chatgpt:1",
        "ask:grok:1",
        "ask:gemini:1",
        "ask:chatgpt:2",
        "ask:grok:2",
        "ask:gemini:2",
    ]
    assert events[-4:] == [
        "close:gemini",
        "close:grok",
        "close:chatgpt",
        "close:safari-context",
    ]
    assert len(set(owner_threads)) == 1

    events.clear()
    owner_threads.clear()
    fail_grok[0] = True
    failed_service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "failed-safari-jury",
        login_check=lambda *_args, **_kwargs: pytest.fail(
            "Safari start must not open disposable preflight sessions."
        ),
    )
    try:
        failed_session_id = failed_service.start(
            "safari",
            ["chatgpt", "grok"],
            "Preserve the completed first vote.",
        )["session_id"]
        wait_until(lambda: not failed_service.status(failed_session_id)["running"])
        failed = failed_service.status(failed_session_id)
    finally:
        failed_service.stop_at_exit()

    assert failed["phase"] == "failed"
    assert [item["provider"] for item in failed["rounds"][0]["opinions"]] == [
        "chatgpt",
    ]
    assert next(
        item for item in failed["providers"] if item["key"] == "grok"
    )["status"] == "failed"
    assert events.count("open:safari-context") == 1
    assert events.count("close:safari-context") == 1
    assert not any("edge" in event for event in events)


def test_safari_account_check_uses_one_context_three_tabs_and_keeps_collecting(
    tmp_path,
    monkeypatch,
):
    from app.core import jury_browser, safari_automation

    events = []
    contexts = []

    class SafariContext:
        def __init__(self, initial_url, *, lock_blocking):
            assert initial_url == "about:blank"
            assert lock_blocking is False
            self.primary_page = object()
            contexts.append(self)

        def __enter__(self):
            events.append("open:safari-context")
            return self

        def new_page(self):
            events.append("new:safari-page")
            return object()

        def __exit__(self, exc_type, exc, traceback):
            events.append("close:safari-context")

    class SafariSession:
        def __init__(self, settings, platform, stop=None, **kwargs):
            assert settings.browser == "safari"
            assert kwargs["browser_page"] is not None
            assert kwargs.get("on_conversation") is None
            self.platform = platform

        def __enter__(self):
            events.append(f"open:{self.platform}")
            if self.platform == "grok":
                raise RuntimeError("Grok composer is unavailable.")
            return self

        def ask(self, prompt, *, timeout_seconds=None):
            raise AssertionError("Safari account check must not send a prompt.")

        def __exit__(self, exc_type, exc, traceback):
            events.append(f"close:{self.platform}")

    monkeypatch.setattr(
        jury_module,
        "browser_options_for_host",
        lambda: ({"key": "edge"}, {"key": "safari"}),
    )
    monkeypatch.setattr(
        safari_automation,
        "verify_safari_context_cleanup_ready",
        lambda: None,
    )
    monkeypatch.setattr(jury_browser, "SafariContext", SafariContext)
    monkeypatch.setattr(jury_browser, "JuryBrowserSession", SafariSession)
    monkeypatch.setattr(
        jury_browser,
        "launch_chromium_context",
        lambda *_args, **_kwargs: pytest.fail("Safari Jury must not fall back to Edge."),
    )
    service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "safari-check",
    )
    checked = service.check("safari", ["chatgpt", "grok", "gemini"])

    assert len(contexts) == 1
    assert events.count("open:safari-context") == 1
    assert events.count("new:safari-page") == 2
    assert events.count("close:safari-context") == 1
    assert [event for event in events if event.startswith("open:") and event != "open:safari-context"] == [
        "open:chatgpt",
        "open:grok",
        "open:gemini",
    ]
    assert checked["ready"] is False
    assert [item["key"] for item in checked["providers"]] == ["chatgpt", "grok", "gemini"]
    by_key = {item["key"]: item for item in checked["providers"]}
    assert by_key["chatgpt"]["ready"] is True
    assert by_key["gemini"]["ready"] is True
    assert by_key["grok"]["ready"] is False
    assert "Grok composer is unavailable" in by_key["grok"]["message"]
    assert "ask:" not in "".join(events)
    assert not any("edge" in event for event in events)


def test_safari_account_check_aligns_results_to_requested_provider_order(
    tmp_path,
    monkeypatch,
):
    from app.core import jury_browser, safari_automation

    providers = ["gemini", "chatgpt", "grok"]
    models = {
        "gemini": "gemini-3.8-flash",
        "chatgpt": "chatgpt-latest-instant",
        "grok": "grok-build",
    }

    monkeypatch.setattr(
        jury_module,
        "browser_options_for_host",
        lambda: ({"key": "edge"}, {"key": "safari"}),
    )
    monkeypatch.setattr(
        safari_automation,
        "verify_safari_context_cleanup_ready",
        lambda: None,
    )
    monkeypatch.setattr(
        jury_browser,
        "jury_safari_account_check",
        lambda *_args, **_kwargs: [
            {"logged_in": False, "message": "Gemini needs sign-in."},
            {"platform": "", "logged_in": True, "message": "Signed in"},
            {"platform": "chatgpt", "logged_in": False, "message": "Grok needs sign-in."},
        ],
    )
    service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "safari-check-order",
    )

    checked = service.check("safari", providers, models)

    assert [item["key"] for item in checked["providers"]] == providers
    assert [item["ready"] for item in checked["providers"]] == [False, True, False]
    assert [item["message"] for item in checked["providers"]] == [
        "Gemini needs sign-in.",
        "Signed in",
        "Grok needs sign-in.",
    ]
    assert [item["model_selection"] for item in checked["providers"]] == [
        models[key] for key in providers
    ]


def test_macos_edge_account_check_receives_the_service_owned_profile_root(
    tmp_path,
    monkeypatch,
):
    from app.core import jury_browser

    providers = ["gemini", "chatgpt", "grok"]
    calls = []
    monkeypatch.setattr(jury_module, "is_macos_host", lambda: True)

    def shared_check(settings, selected, selections, **kwargs):
        calls.append((settings.browser, selected, selections, kwargs))
        return [
            {
                "platform": platform,
                "browser": "edge",
                "logged_in": True,
                "message": "Signed in",
            }
            for platform in selected
        ]

    monkeypatch.setattr(jury_browser, "jury_macos_edge_account_check", shared_check)
    monkeypatch.setattr(
        jury_browser,
        "jury_browser_login_check",
        lambda *_args, **_kwargs: pytest.fail(
            "macOS Edge account checks must use the shared project process."
        ),
    )
    service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "agent-runtime" / "jury",
    )

    checked = service.check("edge", providers)

    assert checked["ready"] is True
    assert [item["key"] for item in checked["providers"]] == providers
    assert len(calls) == 1
    assert calls[0][0:2] == ("edge", providers)
    assert calls[0][3]["project_profile_root"] == (
        tmp_path / "agent-runtime" / "agent_browser_profile"
    )


def test_safari_owned_window_closes_after_unavailable_jurors_and_stop(
    tmp_path,
    monkeypatch,
):
    from app.core import jury_browser, safari_automation

    events = []
    entered = Event()

    class SafariContext:
        def __init__(self, initial_url, *, lock_blocking):
            assert lock_blocking is False
            self.primary_page = object()

        def __enter__(self):
            events.append("open:safari-context")
            return self

        def new_page(self):
            events.append("new:safari-page")
            return object()

        def __exit__(self, exc_type, exc, traceback):
            events.append("close:safari-context")

    class UnavailableSession:
        def __init__(self, settings, platform, stop, **kwargs):
            assert settings.browser == "safari"
            self.platform = platform

        def __enter__(self):
            events.append(f"open:{self.platform}")
            raise RuntimeError(f"{self.platform} composer is unavailable.")

        def ask(self, prompt, *, timeout_seconds=None):
            raise AssertionError("Unavailable Safari jurors must not receive a prompt.")

        def __exit__(self, exc_type, exc, traceback):
            events.append(f"close:{self.platform}")

    class StoppableSession:
        def __init__(self, settings, platform, stop, **kwargs):
            assert settings.browser == "safari"
            self.platform = platform
            self.stop = stop
            self.on_conversation = kwargs["on_conversation"]
            self.conversation_url = f"https://{platform}.example/conversation/safari"

        def __enter__(self):
            events.append(f"open:{self.platform}")
            return self

        def ask(self, prompt, *, timeout_seconds=None):
            events.append(f"ask:{self.platform}")
            entered.set()
            assert self.stop.wait(5)
            raise RuntimeError("The jury was stopped.")

        def __exit__(self, exc_type, exc, traceback):
            events.append(f"close:{self.platform}")

    monkeypatch.setattr(
        jury_module,
        "browser_options_for_host",
        lambda: ({"key": "edge"}, {"key": "safari"}),
    )
    monkeypatch.setattr(
        safari_automation,
        "verify_safari_context_cleanup_ready",
        lambda: None,
    )
    monkeypatch.setattr(safari_automation, "SafariContext", SafariContext)
    monkeypatch.setattr(jury_browser, "SafariContext", SafariContext)
    monkeypatch.setattr(jury_browser, "JuryBrowserSession", UnavailableSession)
    service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "safari-unavailable",
    )
    try:
        session_id = service.start(
            "safari",
            ["chatgpt", "grok"],
            "Do not send this.",
        )["session_id"]
        wait_until(lambda: not service.status(session_id)["running"])
        failed = service.status(session_id)
    finally:
        service.stop_at_exit()

    assert failed["phase"] == "failed"
    assert events.count("open:safari-context") == 1
    assert events.count("close:safari-context") == 1
    assert not any(event.startswith("ask:") for event in events)

    events.clear()
    monkeypatch.setattr(jury_browser, "JuryBrowserSession", StoppableSession)
    stopped_service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "safari-stop",
    )
    try:
        stopped_id = stopped_service.start(
            "safari",
            ["chatgpt", "grok"],
            "Stop before the first vote.",
        )["session_id"]
        assert entered.wait(5)
        stopped_service.stop(stopped_id)
        wait_until(lambda: not stopped_service.status(stopped_id)["running"])
        stopped = stopped_service.status(stopped_id)
    finally:
        stopped_service.stop_at_exit()

    assert stopped["phase"] == "stopped"
    assert events.count("open:safari-context") == 1
    assert events.count("close:safari-context") == 1
    assert events[-1] == "close:safari-context"


def test_safari_cleanup_failure_blocks_a_second_jury_before_window_creation(
    tmp_path,
    monkeypatch,
):
    from app.core import jury_browser, safari_automation

    events = []
    cleanup_checks = iter((None, RuntimeError("previous Jury window is still open")))

    def verify_cleanup_ready():
        outcome = next(cleanup_checks)
        if isinstance(outcome, BaseException):
            raise outcome

    class SafariContext:
        def __init__(self, initial_url, *, lock_blocking):
            assert initial_url == "about:blank"
            assert lock_blocking is False
            self.primary_page = object()

        def __enter__(self):
            events.append("open:safari-context")
            return self

        def new_page(self):
            events.append("new:safari-page")
            return object()

        def __exit__(self, exc_type, exc, traceback):
            events.append("failed-close:safari-context")
            raise RuntimeError("simulated Safari cleanup failure")

    class UnavailableSession:
        def __init__(self, settings, platform, stop, **_kwargs):
            assert settings.browser == "safari"
            self.platform = platform

        def __enter__(self):
            raise RuntimeError(f"{self.platform} composer is unavailable.")

        def __exit__(self, exc_type, exc, traceback):
            return None

    monkeypatch.setattr(
        jury_module,
        "browser_options_for_host",
        lambda: ({"key": "edge"}, {"key": "safari"}),
    )
    monkeypatch.setattr(safari_automation, "SafariContext", SafariContext)
    monkeypatch.setattr(jury_browser, "JuryBrowserSession", UnavailableSession)
    monkeypatch.setattr(
        safari_automation,
        "verify_safari_context_cleanup_ready",
        verify_cleanup_ready,
    )
    service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "safari-cleanup-failure",
    )
    try:
        session_id = service.start(
            "safari",
            ["chatgpt", "grok"],
            "Do not send this.",
        )["session_id"]
        wait_until(lambda: not service.status(session_id)["running"])
        failed = service.status(session_id)

        with pytest.raises(RuntimeError, match="previous Jury window"):
            service.start(
                "safari",
                ["chatgpt", "grok"],
                "Do not create a second task window.",
            )
    finally:
        service.stop_at_exit()

    assert failed["phase"] == "failed"
    assert failed["resource_cleanup_pending"] is True
    assert failed["safari_cleanup_pending"] is True
    assert "simulated Safari cleanup failure" in failed["resource_cleanup_warning"]
    assert events.count("open:safari-context") == 1
    assert len(service.records) == 1


def test_service_exit_waits_for_active_safari_window_cleanup(
    tmp_path,
    monkeypatch,
):
    from app.core import jury_browser, safari_automation

    events = []
    entered = Event()

    class SafariContext:
        def __init__(self, initial_url, *, lock_blocking):
            assert initial_url == "about:blank"
            assert lock_blocking is False
            self.primary_page = object()

        def __enter__(self):
            events.append("open:safari-context")
            return self

        def new_page(self):
            events.append("new:safari-page")
            return object()

        def __exit__(self, exc_type, exc, traceback):
            events.append("close:safari-context")

    class StoppableSession:
        def __init__(self, settings, platform, stop, **kwargs):
            assert settings.browser == "safari"
            self.platform = platform
            self.stop = stop
            self.conversation_url = ""

        def __enter__(self):
            events.append(f"open:{self.platform}")
            return self

        def ask(self, prompt, *, timeout_seconds=None):
            events.append(f"ask:{self.platform}")
            entered.set()
            assert self.stop.wait(5)
            raise RuntimeError("The service is exiting.")

        def __exit__(self, exc_type, exc, traceback):
            events.append(f"close:{self.platform}")

    monkeypatch.setattr(
        jury_module,
        "browser_options_for_host",
        lambda: ({"key": "edge"}, {"key": "safari"}),
    )
    monkeypatch.setattr(
        safari_automation,
        "verify_safari_context_cleanup_ready",
        lambda: None,
    )
    monkeypatch.setattr(safari_automation, "SafariContext", SafariContext)
    monkeypatch.setattr(jury_browser, "JuryBrowserSession", StoppableSession)
    service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "safari-exit",
    )
    session_id = service.start(
        "safari",
        ["chatgpt", "grok"],
        "Stop during the first vote.",
    )["session_id"]
    assert entered.wait(5)

    service.stop_at_exit()

    final = service.status(session_id)
    assert final["running"] is False
    assert final["phase"] == "interrupted"
    assert final["termination_reason"] == "service_shutdown"
    assert events.count("open:safari-context") == 1
    assert events.count("close:safari-context") == 1
    assert events[-1] == "close:safari-context"


def test_service_exit_closes_admission_and_waits_for_safari_account_check(
    tmp_path,
    monkeypatch,
):
    from app.core import jury_browser, safari_automation

    entered = Event()
    canceled = Event()
    release = Event()
    check_finished = Event()
    shutdown_finished = Event()
    check_results = []

    def safari_check(settings, providers, selections, *, config, stop_event):
        assert settings.browser == "safari"
        assert providers == ["chatgpt", "grok", "gemini"]
        assert set(selections) == set(providers)
        assert config is not None
        entered.set()
        assert stop_event.wait(5)
        canceled.set()
        assert release.wait(5)
        return [
            {
                "platform": provider,
                "logged_in": False,
                "message": "Jury shutdown canceled the account check.",
            }
            for provider in providers
        ]

    monkeypatch.setattr(
        jury_module,
        "browser_options_for_host",
        lambda: ({"key": "edge"}, {"key": "safari"}),
    )
    monkeypatch.setattr(
        safari_automation,
        "verify_safari_context_cleanup_ready",
        lambda: None,
    )
    monkeypatch.setattr(jury_browser, "jury_safari_account_check", safari_check)
    service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "safari-check-shutdown",
        session_factory=FakeBrowserFactory(),
    )

    def run_check():
        try:
            check_results.append(
                service.check("safari", ["chatgpt", "grok", "gemini"])
            )
        finally:
            check_finished.set()

    def run_shutdown():
        try:
            service.stop_at_exit()
        finally:
            shutdown_finished.set()

    check_thread = Thread(target=run_check)
    shutdown_thread = Thread(target=run_shutdown)
    check_thread.start()
    assert entered.wait(5)
    shutdown_thread.start()
    try:
        assert canceled.wait(5)
        assert shutdown_finished.is_set() is False
        with pytest.raises(RuntimeError, match="shutting down"):
            service.check("edge", ["chatgpt", "grok"])
        with pytest.raises(RuntimeError, match="shutting down"):
            service.start("edge", ["chatgpt", "grok"], "Do not start.")
    finally:
        release.set()
    check_thread.join(timeout=5)
    shutdown_thread.join(timeout=5)

    assert check_finished.is_set()
    assert shutdown_finished.is_set()
    assert check_results[0]["ready"] is False
    assert service._active_account_checks == {}
    assert service.records == {}


def test_selected_model_tiers_are_persisted_and_given_to_each_single_worker(service_factory):
    factory = FakeBrowserFactory()
    models = {
        "chatgpt": "chatgpt-latest-high",
        "gemini": "gemini-3.8-flash",
    }
    final = complete(
        service_factory(factory), ["chatgpt", "gemini"], models=models,
    )
    assert [(item["key"], item["model_selection"], item["model"])
            for item in final["providers"]] == [
        ("chatgpt", "chatgpt-latest-high", "Latest · High"),
        ("gemini", "gemini-3.8-flash", "3.8 Flash"),
    ]
    assert factory.model_selections == {
        "chatgpt": ["chatgpt-latest-high"],
        "gemini": ["gemini-3.8-flash"],
    }


@pytest.mark.parametrize("models", [[], {"claude": "claude-auto"}, {"gemini": "unknown"}])
def test_invalid_model_tier_selection_is_rejected_before_browser_activity(
    service_factory, models,
):
    factory = FakeBrowserFactory()
    service = service_factory(factory)
    with pytest.raises(ValueError):
        service.start("edge", ["chatgpt", "gemini"], "Claim", models=models)
    assert factory.opened == {}


@pytest.mark.parametrize("providers", [None, "chatgpt,grok", [], ["chatgpt"], ["chatgpt", "chatgpt"], ["chatgpt", "unknown"], ["chatgpt", {}]])
def test_invalid_juror_selection_is_rejected(providers):
    with pytest.raises(ValueError):
        validate_selection("edge", providers)


@pytest.mark.parametrize("response", ["plain prose", "[]", "null", '{"verdict": []}', '{"verdict": {}}'])
def test_invalid_model_output_cannot_become_a_vote(response):
    opinion = parse_opinion(response)
    assert opinion["valid"] is False
    assert opinion["verdict"] == "unverified"
    assert build_candidate([opinion, opinion]) is None


@pytest.mark.parametrize("label", ["JSON\n", "json\r\n", "Json \n", "JSON ", "json\t"])
def test_rendered_json_language_label_preserves_a_strict_vote(label):
    opinion = parse_opinion(label + json.dumps(vote()))
    assert opinion["valid"] is True
    assert opinion["verdict"] == "supported"
    assert opinion["evidence"]


@pytest.mark.parametrize(
    "response",
    [
        "JSON explanation\n{}",
        "Before\nJSON\n{}",
        "JSON\n{}\nAfter",
    ],
)
def test_json_language_label_never_extracts_an_object_from_prose(response):
    assert parse_opinion(response)["valid"] is False


def test_same_verdict_requires_explicit_acceptance_of_the_exact_candidate():
    initial = parse_opinion(json.dumps(vote()))
    candidate = build_candidate([initial, initial])
    assert candidate is not None
    assert unanimous_acceptance([initial, initial], candidate) is False
    accepted = parse_opinion(json.dumps(vote(candidate=candidate)))
    assert unanimous_acceptance([accepted, accepted], candidate) is True
    for changes in ({"candidate_id": "stale"}, {"unresolved": ["A source disagrees."]}, {"evidence": []}, {"verdict": "refuted"}):
        dissent = parse_opinion(json.dumps(vote(candidate=candidate, **changes)))
        assert unanimous_acceptance([accepted, dissent], candidate) is False


def test_material_signature_detects_new_support_from_the_same_source_url():
    old = [{**parse_opinion(json.dumps(vote())), "provider": "chatgpt"}]
    revised = [{
        **parse_opinion(json.dumps(vote(evidence=[{
            "url": "https://source.example/report",
            "supports": "A corrected table reverses the claimed causal direction.",
        }]))),
        "provider": "chatgpt",
    }]
    assert deliberation_signature(old) != deliberation_signature(revised)


def test_material_signature_ignores_arbitrary_candidate_ids_but_tracks_exact_acceptance():
    candidate = build_candidate([
        parse_opinion(json.dumps(vote())),
        parse_opinion(json.dumps(vote())),
    ])
    assert candidate is not None
    wrong_one = [{
        **parse_opinion(json.dumps(vote(
            accept_candidate=True,
            candidate_id="invented-one",
        ))),
        "provider": "chatgpt",
    }]
    wrong_two = [{
        **parse_opinion(json.dumps(vote(
            accept_candidate=True,
            candidate_id="invented-two",
        ))),
        "provider": "chatgpt",
    }]
    accepted = [{
        **parse_opinion(json.dumps(vote(candidate=candidate))),
        "provider": "chatgpt",
    }]
    assert deliberation_signature(wrong_one, candidate) == deliberation_signature(
        wrong_two, candidate,
    )
    assert deliberation_signature(wrong_one, candidate) != deliberation_signature(
        accepted, candidate,
    )


def test_multiple_rounds_share_one_session_per_provider_and_one_prior_round_packet(service_factory):
    factory = FakeBrowserFactory()
    service = service_factory(factory)
    final = complete(service, list(DEFAULT_JURORS))
    assert final["phase"] == "consensus"
    assert final["consensus"] is True
    assert final["round"] == 2
    assert factory.opened == factory.closed == Counter(dict.fromkeys(DEFAULT_JURORS, 1))
    for key in DEFAULT_JURORS:
        assert len(factory.packets[key]) == 2
        assert len(set(factory.threads[key])) == 1
        assert factory.packets[key][0]["previous_round"] == []
        assert factory.packets[key][0]["candidate"] is None
        assert factory.packets[key][1] == factory.packets["chatgpt"][1]
        assert [item["provider"] for item in factory.packets[key][1]["previous_round"]] == list(DEFAULT_JURORS)
    assert len({factory.threads[key][0] for key in DEFAULT_JURORS}) == 3
    assert len({item["conversation_url"] for item in final["providers"]}) == 3


def test_settled_disagreement_converges_without_false_consensus_or_new_sessions(service_factory):
    factory = FakeBrowserFactory(lambda key, packet, stop: vote(verdict="supported" if key == "chatgpt" else "refuted"))
    final = complete(service_factory(factory))
    assert final["phase"] == "inconclusive"
    assert final["consensus"] is False
    assert final["termination_reason"] == "cross_review_complete"
    assert len(final["rounds"]) == 2
    assert factory.opened == factory.closed == {"chatgpt": 1, "grok": 1}
    assert all(len(packets) == 2 for packets in factory.packets.values())
    assert {item["verdict"] for item in final["rounds"][-1]["opinions"]} == {"supported", "refuted"}


def test_automatic_convergence_can_continue_beyond_the_old_three_round_default(service_factory):
    counts = Counter()

    def answer(key, packet, stop):
        counts[key] += 1
        number = counts[key]
        if number <= 3:
            return vote(
                verdict="supported" if key == "chatgpt" else "refuted",
                evidence=[{
                    "url": f"https://source.example/review-{number}",
                    "supports": f"Review pass {number} adds a primary source.",
                }],
                unresolved=[f"Review source set {number + 1}."],
            )
        return vote(candidate=packet["candidate"])

    factory = FakeBrowserFactory(answer)
    final = complete(service_factory(factory))
    assert final["phase"] == "consensus"
    assert final["termination_reason"] == "unanimous_acceptance"
    assert final["round"] == 5
    assert all(len(packets) == 5 for packets in factory.packets.values())
    assert factory.opened == factory.closed == {"chatgpt": 1, "grok": 1}


def test_explicit_legacy_round_budget_keeps_its_bounded_semantics(service_factory):
    counts = Counter()

    def answer(key, packet, stop):
        counts[key] += 1
        return vote(
            verdict="supported" if key == "chatgpt" else "refuted",
            evidence=[{
                "url": f"https://source.example/review-{counts[key]}",
                "supports": "The legacy client requested another bounded review pass.",
            }],
            unresolved=["Continue until the legacy round budget is exhausted."],
        )

    service = service_factory(FakeBrowserFactory(answer))
    models = {"chatgpt": "chatgpt-latest-high", "grok": "grok-auto"}
    initial = service.start(
        "edge", ["chatgpt", "grok"], "Check the legacy client claim.", 3, models,
    )
    wait_until(lambda: not service.status(initial["session_id"])["running"])
    final = service.status(initial["session_id"])
    assert final["convergence_mode"] == "bounded"
    assert final["max_rounds"] == 3
    assert final["termination_reason"] == "round_limit"
    assert final["round"] == 3


def test_storage_boundary_stops_before_sending_another_automatic_round(
    service_factory, monkeypatch,
):
    def record_size(record):
        if (
            record.get("rounds")
            and len(record["rounds"][-1]["opinions"]) == len(record["providers"])
        ):
            return jury_module.AUTOMATIC_CONVERGENCE_STORAGE_SOFT_LIMIT_BYTES
        return 0

    monkeypatch.setattr(jury_module, "_serialized_record_size", record_size)
    factory = FakeBrowserFactory(lambda key, packet, stop: vote(
        verdict="supported" if key == "chatgpt" else "refuted",
        unresolved=["A material objection remains."],
    ))
    final = complete(service_factory(factory))
    assert final["phase"] == "inconclusive"
    assert final["termination_reason"] == "storage_safety_boundary"
    assert final["round"] == 1
    assert all(len(packets) == 1 for packets in factory.packets.values())


def test_wall_clock_boundary_stops_pending_workers_without_a_provider_failure(
    service_factory, monkeypatch,
):
    monkeypatch.setattr(jury_module, "AUTOMATIC_CONVERGENCE_TIMEOUT_SECONDS", 0.05)

    def answer(key, packet, shutdown):
        assert shutdown.wait(5)
        return vote()

    factory = FakeBrowserFactory(answer)
    final = complete(service_factory(factory))
    assert final["phase"] == "inconclusive"
    assert final["termination_reason"] == "time_safety_boundary"
    assert final["round"] == 1
    assert all(item["status"] != "failed" for item in final["providers"])
    assert factory.opened == factory.closed == {"chatgpt": 1, "grok": 1}


def test_complete_consensus_wins_when_the_clock_expires_before_future_harvest(
    service_factory, monkeypatch,
):
    expired = Event()
    calls = Counter()

    class ClosedThread:
        @staticmethod
        def is_alive():
            return False

        @staticmethod
        def join(timeout=None):
            return None

    class ImmediateWorker:
        def __init__(self, factory, settings, platform, stop, config,
                     on_conversation, model_selection):
            self.platform = platform
            self.thread = ClosedThread()
            self.closed = True
            self.error = None
            self.conversation_url = f"https://{platform}.example/conversation/one"

        def ask(self, prompt, timeout_seconds):
            packet = json.loads(prompt.split("Evidence packet (JSON):\n", 1)[1])
            calls[self.platform] += 1
            if all(calls[key] >= 2 for key in ("chatgpt", "grok")):
                expired.set()
            response = vote(candidate=packet["candidate"])
            future = Future()
            future.set_result((json.dumps(response), self.conversation_url))
            return future

        def close(self):
            return None

    monkeypatch.setattr(jury_module, "_JurorWorker", ImmediateWorker)
    monkeypatch.setattr(
        jury_module, "monotonic", lambda: 3_601.0 if expired.is_set() else 0.0,
    )
    final = complete(service_factory())
    assert final["phase"] == "consensus"
    assert final["termination_reason"] == "unanimous_acceptance"
    assert final["round"] == 2


def test_repeated_unresolved_evidence_stops_as_a_visible_stall(service_factory):
    factory = FakeBrowserFactory(lambda key, packet, stop: vote(
        verdict="supported" if key == "chatgpt" else "refuted",
        unresolved=["The primary archives remain inconsistent."],
    ))
    final = complete(service_factory(factory))
    assert final["phase"] == "inconclusive"
    assert final["termination_reason"] == "evidence_stalled"
    assert final["round"] == 2
    assert all(len(packets) == 2 for packets in factory.packets.values())


def test_new_source_urls_reset_stall_detection_before_it_settles(service_factory):
    counts = Counter()

    def answer(key, packet, stop):
        counts[key] += 1
        source_number = min(counts[key], 2)
        return vote(
            verdict="supported" if key == "chatgpt" else "refuted",
            evidence=[{
                "url": f"https://source.example/review-{source_number}",
                "supports": "The primary record remains disputed.",
            }],
            unresolved=["The primary archives remain inconsistent."],
        )

    final = complete(service_factory(FakeBrowserFactory(answer)))
    assert final["phase"] == "inconclusive"
    assert final["termination_reason"] == "evidence_stalled"
    assert final["round"] == 3


def test_third_juror_objection_blocks_two_accepting_jurors_until_reviewed(service_factory):
    counts = Counter()

    def answer(key, packet, stop):
        counts[key] += 1
        return vote(
            candidate=packet["candidate"],
            unresolved=["Verify the original publication date."]
            if key == "gemini" and counts[key] <= 2
            else [],
        )

    factory = FakeBrowserFactory(answer)
    final = complete(service_factory(factory), list(DEFAULT_JURORS))
    assert final["phase"] == "consensus"
    assert final["round"] == 3
    assert all(item["accept_candidate"] for item in final["rounds"][1]["opinions"])
    assert final["rounds"][1]["opinions"][2]["unresolved"]
    assert final["rounds"][2]["opinions"][2]["unresolved"] == []
    assert all(len(factory.packets[key][1]["previous_round"]) == 3 for key in DEFAULT_JURORS)
    assert factory.opened == factory.closed == Counter(dict.fromkeys(DEFAULT_JURORS, 1))


def test_malformed_first_vote_can_recover_in_the_same_provider_session(service_factory):
    counts = Counter()

    def answer(key, packet, stop):
        counts[key] += 1
        if key == "grok" and counts[key] == 1:
            return '{"verdict": {}}'
        return vote(candidate=packet["candidate"])

    factory = FakeBrowserFactory(answer)
    final = complete(service_factory(factory))
    assert final["phase"] == "consensus"
    assert final["round"] == 3
    assert factory.opened == {"chatgpt": 1, "grok": 1}
    assert final["rounds"][0]["opinions"][1]["valid"] is False


def test_rendered_json_language_label_can_reach_exact_candidate_acceptance(
    service_factory,
):
    factory = FakeBrowserFactory(lambda key, packet, stop: (
        "JSON\n" + json.dumps(vote(candidate=packet["candidate"]))
    ))
    final = complete(service_factory(factory))
    assert final["phase"] == "consensus"
    assert final["termination_reason"] == "unanimous_acceptance"
    assert final["round"] == 2
    assert all(
        opinion["valid"]
        for round_record in final["rounds"]
        for opinion in round_record["opinions"]
    )


def test_repeated_malformed_votes_are_not_reported_as_evidence_disagreement(
    service_factory,
):
    factory = FakeBrowserFactory(lambda key, packet, stop: '{"verdict": {}}')
    final = complete(service_factory(factory))
    assert final["phase"] == "inconclusive"
    assert final["termination_reason"] == "structured_vote_stalled"
    assert final["round"] == 2
    assert "structured votes" in final["response"]
    assert "disagreement" not in final["message"].lower()


def test_any_selected_login_failure_prevents_all_provider_messages(service_factory):
    factory = FakeBrowserFactory()
    service = service_factory(factory, lambda settings, key, **kwargs: {"logged_in": key != "gemini", "message": "Unavailable"})
    final = complete(service, list(DEFAULT_JURORS))
    assert final["phase"] == "failed"
    assert final["consensus"] is False
    assert next(item for item in final["providers"] if item["key"] == "gemini")["ready"] is False
    assert factory.opened == {}
    assert factory.packets == {}


def test_readiness_requires_every_selected_login_without_probing_unselected_claude(service_factory):
    checked = []

    def probe(settings, key, **kwargs):
        checked.append(key)
        return {"logged_in": key != "gemini"}

    service = service_factory(login_check=probe)
    unavailable = service.check("edge", list(DEFAULT_JURORS))
    assert unavailable["ready"] is False
    assert unavailable["message"] == (
        "Unavailable juror: Gemini (3.1 Pro) — Sign-in could not be verified. "
        "Sign in or deselect this juror, then check accounts again; at least two "
        "must remain selected."
    )
    assert checked == list(DEFAULT_JURORS)
    assert "claude" not in checked
    checked.clear()
    ready = service.check("edge", ["chatgpt", "grok"])
    assert ready["ready"] is True
    assert checked == ["chatgpt", "grok"]


def test_readiness_checks_the_selected_provider_model(service_factory):
    selections = []

    def probe(_settings, key, **kwargs):
        selections.append((key, kwargs["model_selection"]))
        return {"logged_in": True}

    service = service_factory(login_check=probe)
    ready = service.check(
        "edge",
        ["chatgpt", "gemini"],
        {
            "chatgpt": "chatgpt-latest-high",
            "gemini": "gemini-3.8-flash",
        },
    )
    assert ready["ready"] is True
    assert selections == [
        ("chatgpt", "chatgpt-latest-high"),
        ("gemini", "gemini-3.8-flash"),
    ]


@pytest.mark.parametrize("question", [None, {}, "", "  ", "x" * 20_001])
def test_invalid_question_cannot_allocate_a_jury_or_open_a_browser(service_factory, question):
    factory = FakeBrowserFactory()
    service = service_factory(factory)
    with pytest.raises(ValueError):
        service.start("edge", ["chatgpt", "grok"], question)
    assert service.records == {}
    assert factory.opened == {}


def test_new_jury_records_use_automatic_convergence_without_a_round_budget(service_factory):
    service = service_factory()
    initial = service.start("edge", ["chatgpt", "grok"], "Claim")
    assert initial["convergence_mode"] == "automatic"
    assert initial["termination_reason"] == ""
    assert "max_rounds" not in initial


@pytest.mark.parametrize("rounds", [True, "3", 1, 7])
def test_legacy_round_budget_remains_a_bounded_integer(service_factory, rounds):
    service = service_factory()
    with pytest.raises(ValueError):
        service.start("edge", ["chatgpt", "grok"], "Claim", rounds)
    assert service.records == {}


@pytest.mark.parametrize("peer_failure", [False, True])
def test_cancellation_retains_admission_until_all_browser_owners_close(service_factory, peer_failure):
    barrier = Barrier(2)
    released = Event()
    entered = Event()

    def answer(key, packet, stop):
        barrier.wait(timeout=5)
        entered.set()
        if peer_failure and key == "grok":
            raise RuntimeError("Provider failed after opening its only conversation.")
        assert stop.wait(5)
        return vote()

    factory = FakeBrowserFactory(answer, close_wait=released)
    service = service_factory(factory)
    session_id = service.start("edge", ["chatgpt", "grok"], "Check the claim.")["session_id"]
    try:
        assert entered.wait(5)
        if not peer_failure:
            service.stop(session_id)
        stop_event = (
            service.shutdowns[session_id]
            if peer_failure
            else service.stops[session_id]
        )
        wait_until(stop_event.is_set)
        if peer_failure:
            assert service.stops[session_id].is_set() is False
        assert service.status(session_id)["running"] is True
        with pytest.raises(RuntimeError, match="already running"):
            service.start("edge", ["chatgpt", "grok"], "Do not start a replacement.")
    finally:
        released.set()
    wait_until(lambda: not service.status(session_id)["running"])
    assert service.status(session_id)["phase"] == ("failed" if peer_failure else "stopped")
    assert factory.opened == factory.closed == {"chatgpt": 1, "grok": 1}
    assert all(len(packets) == 1 for packets in factory.packets.values())
    if peer_failure:
        failed = next(item for item in service.status(session_id)["providers"] if item["key"] == "grok")
        assert failed["conversation_url"] == "https://grok.example/conversation/one"


def test_terminal_consensus_cannot_be_overwritten_while_browser_cleanup_is_pending(
    service_factory, monkeypatch,
):
    monkeypatch.setattr(jury_module, "WORKER_SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    released = Event()
    factory = FakeBrowserFactory(close_wait=released)
    service = service_factory(factory)
    session_id = service.start("edge", ["chatgpt", "grok"], "Check the claim.")[
        "session_id"
    ]
    try:
        wait_until(lambda: service.status(session_id)["phase"] == "consensus")
        wait_until(lambda: service.status(session_id).get("resource_cleanup_pending") is True)
        assert service.status(session_id)["running"] is True
        stopped = service.stop(session_id)
        assert stopped["phase"] == "consensus"
        assert service.stops[session_id].is_set() is False
        with pytest.raises(RuntimeError, match="already running"):
            service.start("edge", ["chatgpt", "grok"], "Do not overlap owners.")
    finally:
        released.set()
    wait_until(lambda: not service.status(session_id)["running"])
    assert service.status(session_id)["phase"] == "consensus"
    assert service.status(session_id)["termination_reason"] == "unanimous_acceptance"


def test_project_edge_page_cleanup_failure_remains_visible(service_factory):
    factory = FakeBrowserFactory(
        close_error=RuntimeError("simulated project Edge Page close failure")
    )
    service = service_factory(factory)

    final = complete(service)

    assert final["phase"] == "consensus"
    assert final["resource_cleanup_pending"] is True
    assert "Jury browser Page cleanup failed" in final["resource_cleanup_warning"]
    assert "simulated project Edge Page close failure" in final[
        "resource_cleanup_warning"
    ]
    assert factory.opened == factory.closed == {"chatgpt": 1, "grok": 1}


def test_project_edge_cleanup_failure_is_retained_with_provider_failure(
    service_factory,
):
    def fail_provider(_key, _packet, _shutdown):
        raise RuntimeError("simulated provider failure")

    factory = FakeBrowserFactory(
        fail_provider,
        close_error=RuntimeError("simulated concurrent Page close failure"),
    )
    service = service_factory(factory)

    final = complete(service)

    assert final["phase"] == "failed"
    assert "simulated provider failure" in final["message"]
    assert final["resource_cleanup_pending"] is True
    assert "simulated concurrent Page close failure" in final[
        "resource_cleanup_warning"
    ]
    assert factory.opened == factory.closed == {"chatgpt": 1, "grok": 1}


def test_project_edge_enter_failure_retains_initialization_cleanup_error(
    service_factory,
):
    class EnterFailureFactory:
        def __call__(self, _settings, platform, _stop, **_kwargs):
            class Browser:
                conversation_url = ""
                cleanup_error = RuntimeError(
                    f"simulated {platform} initialization Page close failure"
                )

                def __enter__(self):
                    raise RuntimeError(f"simulated {platform} setup failure")

                def __exit__(self, exc_type, exc, traceback):
                    raise AssertionError("An unentered session must not exit twice.")

            return Browser()

    service = service_factory(EnterFailureFactory())

    final = complete(service)

    assert final["phase"] == "failed"
    assert "setup failure" in final["message"]
    assert final["resource_cleanup_pending"] is True
    assert "initialization Page close failure" in final[
        "resource_cleanup_warning"
    ]


def test_macos_edge_service_exit_waits_for_page_lease_cleanup(
    service_factory,
    monkeypatch,
):
    entered = Event()

    def answer(_key, _packet, shutdown):
        entered.set()
        assert shutdown.wait(5)
        raise RuntimeError("The macOS Edge Jury service is exiting.")

    monkeypatch.setattr(jury_module, "is_macos_host", lambda: True)
    factory = FakeBrowserFactory(answer)
    service = service_factory(factory)
    session_id = service.start("edge", ["chatgpt", "grok"], "Check the claim.")[
        "session_id"
    ]
    assert entered.wait(5)

    service.stop_at_exit()

    final = service.status(session_id)
    assert final["running"] is False
    assert final["phase"] == "interrupted"
    assert final["termination_reason"] == "service_shutdown"
    assert factory.opened == factory.closed == {"chatgpt": 1, "grok": 1}
    assert final["resource_cleanup_pending"] is False


def test_service_shutdown_is_not_misreported_as_a_user_stop(service_factory):
    entered = Event()

    def answer(key, packet, shutdown):
        entered.set()
        assert shutdown.wait(5)
        return vote()

    service = service_factory(FakeBrowserFactory(answer))
    session_id = service.start("edge", ["chatgpt", "grok"], "Check the claim.")[
        "session_id"
    ]
    assert entered.wait(5)
    service.stop_at_exit()
    wait_until(lambda: not service.status(session_id)["running"])
    final = service.status(session_id)
    assert final["phase"] == "interrupted"
    assert final["termination_reason"] == "service_shutdown"
    assert service.stops[session_id].is_set() is False


def test_service_shutdown_during_readiness_failure_remains_interrupted(service_factory):
    entered = Event()
    released = Event()

    def probe(settings, key, **kwargs):
        entered.set()
        assert released.wait(5)
        raise RuntimeError("The provider probe ended during service shutdown.")

    service = service_factory(login_check=probe)
    session_id = service.start("edge", ["chatgpt", "grok"], "Check the claim.")[
        "session_id"
    ]
    shutdown_finished = Event()

    def shutdown_service():
        try:
            service.stop_at_exit()
        finally:
            shutdown_finished.set()

    shutdown_thread = Thread(target=shutdown_service)
    try:
        assert entered.wait(5)
        shutdown_thread.start()
        wait_until(lambda: service._shutdown_started)
        assert shutdown_finished.is_set() is False
    finally:
        released.set()
    shutdown_thread.join(timeout=5)
    assert shutdown_finished.is_set()
    wait_until(lambda: not service.status(session_id)["running"])
    final = service.status(session_id)
    assert final["phase"] == "interrupted"
    assert final["termination_reason"] == "service_shutdown"


def test_reload_recovers_strict_legacy_votes_without_rewriting_the_archive(
    service_factory,
    tmp_path,
):
    root = tmp_path / "legacy-votes"
    root.mkdir()
    session_id = "d" * 32
    raw_votes = (
        "JSON\n" + json.dumps(vote(conclusion="Recovered ChatGPT vote.")),
        "JSON " + json.dumps(vote(conclusion="Recovered Gemini vote.")),
    )
    legacy = {
        "version": "1.5.0",
        "session_id": session_id,
        "browser": "safari",
        "question": "Check the archived claim.",
        "title": "Check the archived claim.",
        "phase": "inconclusive",
        "running": False,
        "consensus": False,
        "termination_reason": "evidence_stalled",
        "message": "Automatic convergence stopped at a stable, visible disagreement.",
        "response": "Review the preserved disagreement below.",
        "round": 1,
        "started_at": "2026-09-16T00:00:00Z",
        "providers": [],
        "rounds": [{
            "round": 1,
            "opinions": [
                {
                    "provider": provider,
                    "valid": False,
                    "verdict": "unverified",
                    "conclusion": response,
                    "evidence": [],
                    "unresolved": ["The juror did not return a structured vote."],
                    "accept_candidate": None,
                    "candidate_id": None,
                    "response": response,
                    "conversation_url": (
                        f"https://{provider}.example/conversation/legacy"
                    ),
                }
                for provider, response in zip(
                    ("chatgpt", "gemini"), raw_votes, strict=True
                )
            ],
        }],
    }
    path = root / f"{session_id}.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    poison = FakeBrowserFactory(
        lambda *_args: pytest.fail(
            "Archive recovery must not send provider messages."
        ),
    )

    restored = service_factory(poison, root=root)
    state = restored.status(session_id)

    opinions = state["rounds"][0]["opinions"]
    assert [item["valid"] for item in opinions] == [True, True]
    assert [item["verdict"] for item in opinions] == ["supported", "supported"]
    assert [item["conclusion"] for item in opinions] == [
        "Recovered ChatGPT vote.",
        "Recovered Gemini vote.",
    ]
    assert all(item["evidence"] and item["unresolved"] == [] for item in opinions)
    assert state["phase"] == "inconclusive"
    assert state["consensus"] is False
    assert state["recovered_structured_vote_count"] == 2
    assert state["termination_reason"] == "archived_vote_recovery"
    assert state["original_termination_reason"] == "evidence_stalled"
    assert "does not rerun deliberation" in state["message"]
    assert "no provider failure" in state["response"]
    assert poison.opened == {}

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["termination_reason"] == "evidence_stalled"
    assert all(
        opinion["valid"] is False
        for opinion in persisted["rounds"][0]["opinions"]
    )


def test_reload_preserves_completed_and_interrupted_juries_without_resending(
    service_factory,
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "durable"
    final = complete(service_factory(root=root))
    interrupted_id = "a" * 32
    interrupted = {
        **final,
        "session_id": interrupted_id,
        "running": True,
        "phase": "discussing",
        "consensus": False,
    }
    interrupted.pop("convergence_mode", None)
    interrupted.pop("termination_reason", None)
    interrupted.pop("resource_cleanup_pending", None)
    (root / f"{interrupted_id}.json").write_text(json.dumps(interrupted), encoding="utf-8")
    cleanup_id = "b" * 32
    cleanup_pending = {
        **final,
        "session_id": cleanup_id,
        "running": True,
        "phase": "consensus",
        "resource_cleanup_pending": True,
    }
    (root / f"{cleanup_id}.json").write_text(
        json.dumps(cleanup_pending), encoding="utf-8",
    )
    safari_cleanup_id = "c" * 32
    safari_cleanup_pending = {
        **final,
        "session_id": safari_cleanup_id,
        "browser": "safari",
        "running": True,
        "phase": "consensus",
        "resource_cleanup_pending": True,
        "safari_cleanup_pending": True,
        "safari_cleanup_owner_pid": 123,
    }
    (root / f"{safari_cleanup_id}.json").write_text(
        json.dumps(safari_cleanup_pending), encoding="utf-8",
    )
    poison = FakeBrowserFactory(lambda *args: pytest.fail("Reload must not send provider messages."))
    restored = service_factory(poison, root=root)
    assert restored.status(final["session_id"])["response"] == final["response"]
    assert restored.status(final["session_id"])["phase"] == "consensus"
    state = restored.status(interrupted_id)
    assert state["phase"] == "interrupted"
    assert state["termination_reason"] == "service_restarted"
    assert state["running"] is False
    assert state["providers"] == final["providers"]
    recovered_cleanup = restored.status(cleanup_id)
    assert recovered_cleanup["phase"] == "consensus"
    assert recovered_cleanup["consensus"] is True
    assert recovered_cleanup["termination_reason"] == "unanimous_acceptance"
    assert recovered_cleanup["resource_cleanup_pending"] is False
    recovered_safari_cleanup = restored.status(safari_cleanup_id)
    assert recovered_safari_cleanup["safari_cleanup_pending"] is True
    assert recovered_safari_cleanup["resource_cleanup_pending"] is True

    from app.core import safari_automation

    monkeypatch.setattr(
        safari_automation,
        "verify_safari_context_cleanup_ready",
        lambda: None,
    )
    restored._require_safari_cleanup_ready()
    recovered_safari_cleanup = restored.status(safari_cleanup_id)
    assert recovered_safari_cleanup["safari_cleanup_pending"] is False
    assert recovered_safari_cleanup["resource_cleanup_pending"] is False
    assert recovered_safari_cleanup["safari_cleanup_owner_pid"] is None
    assert poison.opened == {}
    assert restored.threads == {}
    snapshot = restored.status(final["session_id"])
    snapshot["providers"].clear()
    assert restored.status(final["session_id"])["providers"]


def test_bound_conversation_urls_are_durable_before_any_response_returns(service_factory):
    released = Event()

    def answer(key, packet, stop):
        assert released.wait(5)
        return vote(candidate=packet["candidate"])

    service = service_factory(FakeBrowserFactory(answer))
    session_id = service.start("edge", ["chatgpt", "grok"], "Check this claim.")["session_id"]
    try:
        wait_until(lambda: all(item["conversation_url"] for item in service.status(session_id)["providers"]))
        persisted = json.loads((service.root / f"{session_id}.json").read_text(encoding="utf-8"))
        assert persisted["running"] is True
        assert persisted["consensus"] is False
        assert {item["conversation_url"] for item in persisted["providers"]} == {
            "https://chatgpt.example/conversation/one", "https://grok.example/conversation/one",
        }
        assert not any(item["opinions"] for item in persisted["rounds"])
    finally:
        released.set()
    wait_until(lambda: not service.status(session_id)["running"])


def test_failed_jury_record_can_be_deleted_only_after_all_cleanup_finishes(tmp_path):
    service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "jury-dismissal",
    )
    session_id = "a" * 32
    record = {
        "version": "1.7.1",
        "session_id": session_id,
        "browser": "safari",
        "question": "Check this claim.",
        "title": "Failed Jury",
        "phase": "failed",
        "running": False,
        "resource_cleanup_pending": True,
        "safari_cleanup_pending": True,
        "started_at": "2026-09-16T00:00:00+00:00",
    }
    service._save(record)
    service.records[session_id] = record
    service.stops[session_id] = Event()
    service.shutdowns[session_id] = Event()
    service.threads[session_id] = _ThreadState(True)

    summary = service.sessions("safari")["sessions"][0]
    assert summary["deletable"] is False
    with pytest.raises(RuntimeError, match="cleanup to finish"):
        service.dismiss_failed(session_id)

    record.update(resource_cleanup_pending=False, safari_cleanup_pending=False)
    service.threads[session_id].alive = False
    assert service.sessions("safari")["sessions"][0]["deletable"] is True
    assert service.dismiss_failed(session_id) == {"session_id": session_id}
    assert not (service.root / f"{session_id}.json").exists()
    assert session_id not in service.records
    assert session_id not in service.stops
    assert session_id not in service.shutdowns
    assert session_id not in service.threads


def test_jury_dismissal_rejects_unknown_and_nonfailed_records(tmp_path):
    service = JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        tmp_path / "jury-dismissal-guard",
    )
    session_id = "b" * 32
    service.records[session_id] = {
        "session_id": session_id,
        "browser": "edge",
        "phase": "inconclusive",
        "running": False,
        "started_at": "2026-09-16T00:00:00+00:00",
    }

    with pytest.raises(RuntimeError, match="Only failed"):
        service.dismiss_failed(session_id)
    with pytest.raises(ValueError, match="Unknown jury session"):
        service.dismiss_failed("missing")
