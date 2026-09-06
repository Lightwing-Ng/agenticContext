"""Outstanding provider turn recovery contracts. Code version: v1.0.0-codex.1."""

from types import SimpleNamespace

import pytest

from app.core import computer_use_agent as agent


def test_network_recovery_preserves_binding_and_never_resends(monkeypatch):
    target = "https://chatgpt.com/c/outstanding"
    submitted = []
    states = []
    clock = [0.0]
    reads = [0]
    gates = [0]
    monkeypatch.setattr(agent.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(agent, "WEB_RESPONSE_MINIMUM_SECONDS", 0)
    monkeypatch.setattr(agent, "WEB_RESPONSE_STABLE_SECONDS", 0)
    monkeypatch.setattr(agent, "_submit_chromium_prompt", lambda *args, **kwargs: submitted.append(args[1]))

    def snapshot(*_args):
        reads[0] += 1
        if reads[0] == 1:
            return {"url": target, "count": 0, "text": ""}
        assert any(state.get("conversation_bound") for state in states)
        if reads[0] == 2:
            raise ConnectionError("connection reset by peer")
        return {"url": target, "count": 1, "text": '{"action":"read"}',
                "generating": False, "assistantAfterLatestUser": True}

    def availability():
        gates[0] += 1
        if gates[0] == 2:
            raise TimeoutError("Transport timed out")
        return True

    monkeypatch.setattr(agent, "_chatgpt_response_snapshot", snapshot)
    result = agent._submit_and_wait(
        SimpleNamespace(url=target), "chromium", "Original prompt", lambda: False,
        session_recover=lambda _stop: target, submission_target_url=target,
        availability_check=availability, on_response_state=lambda **state: states.append(state),
    )
    assert result == '{"action":"read"}'
    assert submitted == ["Original prompt"]
    assert any(state["phase"] == "reconnecting" for state in states)
    assert states[-1]["phase"] == "running"


def test_exhausted_transport_is_recoverable_interruption(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda _seconds: None)
    attempts = []

    def read():
        attempts.append(1)
        raise ConnectionError("connection reset")

    with pytest.raises(agent.AgentConnectionInterrupted, match="prompt was not resent"):
        agent._run_recoverable_provider_read(
            read, page=object(), platform="chatgpt", availability_check=None,
            should_stop=lambda: False,
        )
    assert len(attempts) == 61


def test_reconnect_honors_stop_and_rejects_foreign_session(monkeypatch):
    stopped = [False]
    monkeypatch.setattr(agent.time, "sleep", lambda _seconds: stopped.__setitem__(0, True))

    def broken_read():
        raise ConnectionError("connection closed")

    available, _, _ = agent._run_recoverable_provider_read(
        broken_read, page=object(), platform="chatgpt", availability_check=None,
        should_stop=lambda: stopped[0],
    )
    assert not available

    def foreign_read():
        raise RuntimeError("The selected provider tab identity changed")

    with pytest.raises(RuntimeError, match="identity changed"):
        agent._run_recoverable_provider_read(
            foreign_read, page=object(), platform="chatgpt", availability_check=None,
            should_stop=lambda: False,
        )


def test_connection_interruption_keeps_continue_available_at_zero_turns(tmp_path):
    import time

    from app.core.foundation import CrawlConfig

    workspace = tmp_path / "project"
    workspace.mkdir()

    def runner(**kwargs):
        kwargs["update"](conversation_url="https://chatgpt.com/c/recover-zero-turns",
                         conversation_bound=True, turn_count=0)
        raise agent.AgentConnectionInterrupted("Provider connection closed")

    service = agent.ComputerUseAgentService(
        agent.ComputerUseSettingsStore(tmp_path / "settings.json"), runner=runner,
        runtime_root=tmp_path / "runtime",
    )
    try:
        service.start("Inspect the project", str(workspace), CrawlConfig())
        deadline = time.monotonic() + 3
        while service.snapshot()["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        snapshot = service.snapshot()
        assert snapshot["phase"] == "interrupted"
        assert snapshot["turn_count"] == 0
        assert snapshot["conversation_bound"]
        assert next(action for action in service.doctor()["actions"] if action["id"] == "continue")["enabled"]
        assert service.doctor()["events"][-1]["kind"] == "run.interrupted"
    finally:
        service.stop_at_exit()
