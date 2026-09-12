"""Focused tests for the LAN Agent password gate."""

# Code version: v1.1.0-codex.1

from __future__ import annotations

import pytest

from app.core.agent_access_security import (
    agent_access_password_is_configured,
    is_allowed_agent_network_request,
    is_loopback_or_private_address,
    resolve_agent_access_password,
    validate_agent_access_password,
)


def test_agent_password_fails_closed_without_an_explicit_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTIC_CONTEXT_AGENT_PASSWORD", raising=False)
    monkeypatch.delenv("CACHELIKES_AGENT_PASSWORD", raising=False)

    assert resolve_agent_access_password() == ""
    assert not agent_access_password_is_configured()
    assert not validate_agent_access_password("195135")
    assert not validate_agent_access_password("")


def test_agent_password_can_be_overridden_without_changing_the_ui_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTIC_CONTEXT_AGENT_PASSWORD", "246810")

    assert resolve_agent_access_password() == "246810"
    assert agent_access_password_is_configured()
    assert validate_agent_access_password("246810")
    assert not validate_agent_access_password("195135")


def test_agent_password_keeps_the_legacy_environment_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTIC_CONTEXT_AGENT_PASSWORD", raising=False)
    monkeypatch.setenv("CACHELIKES_AGENT_PASSWORD", "135790")

    assert resolve_agent_access_password() == "135790"


@pytest.mark.parametrize("password", ("12345", "1234567", "abcdef", "１２３４５６"))
def test_agent_password_rejects_invalid_explicit_configurations(
    monkeypatch: pytest.MonkeyPatch,
    password: str,
) -> None:
    monkeypatch.setenv("AGENTIC_CONTEXT_AGENT_PASSWORD", password)

    assert not agent_access_password_is_configured()
    assert not validate_agent_access_password(password)


@pytest.mark.parametrize(
    ("address", "expected"),
    (
        ("localhost", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("10.10.10.10", True),
        ("172.16.10.10", True),
        ("192.168.124.10", True),
        ("fc00::10", True),
        ("192.0.2.1", False),
        ("198.51.100.1", False),
        ("203.0.113.1", False),
        ("169.254.1.1", False),
        ("example.com", False),
        ("", False),
        (None, False),
    ),
)
def test_agent_network_address_scope(address: str | None, expected: bool) -> None:
    assert is_loopback_or_private_address(address) is expected


@pytest.mark.parametrize(
    ("remote_addr", "host_name", "expected"),
    (
        ("192.168.124.20", "192.168.124.10", True),
        ("10.10.10.20", "10.10.10.10", True),
        ("127.0.0.1", "localhost", True),
        ("192.0.2.20", "192.168.124.10", False),
        ("192.168.124.20", "malicious.example", False),
        ("192.168.124.20", "203.0.113.10", False),
    ),
)
def test_agent_network_request_requires_private_remote_and_host(
    remote_addr: str,
    host_name: str,
    expected: bool,
) -> None:
    assert is_allowed_agent_network_request(remote_addr, host_name) is expected
