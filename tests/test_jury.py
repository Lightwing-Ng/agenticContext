"""Jury deliberation boundaries with deterministic, browser-free jurors.

Code version: v1.2.1-codex.1
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import Future
import json
from threading import Barrier, Event, Lock, get_ident
import time

import pytest

from app.core import jury as jury_module
from app.core.computer_use_agent import ComputerUseSettings
from app.core.config import CrawlConfig
from app.core.jury import (
    DEFAULT_JURORS,
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

    def __init__(self, answer=None, close_wait=None):
        self.answer = answer or (lambda key, packet, stop: vote(candidate=packet["candidate"]))
        self.close_wait = close_wait
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
    assert validate_selection("edge", ["chatgpt", "claude"]) == ("edge", ["chatgpt", "claude"])
    defaults = validate_model_selections(list(DEFAULT_JURORS), None)
    assert {key: value["selection_key"] for key, value in defaults.items()} == {
        "chatgpt": "chatgpt-latest-extra-high",
        "grok": "grok-auto",
        "gemini": "gemini-3.1-pro",
    }


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
    try:
        assert entered.wait(5)
        service.stop_at_exit()
    finally:
        released.set()
    wait_until(lambda: not service.status(session_id)["running"])
    final = service.status(session_id)
    assert final["phase"] == "interrupted"
    assert final["termination_reason"] == "service_shutdown"


def test_reload_preserves_completed_and_interrupted_juries_without_resending(service_factory, tmp_path):
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
