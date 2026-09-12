"""Security helpers for exposing the local Agent control plane on a LAN."""

# Code version: v1.1.0-codex.1

from __future__ import annotations

import os
import secrets
from ipaddress import ip_address, ip_network


AGENT_ACCESS_PASSWORD_ENV = "AGENTIC_CONTEXT_AGENT_PASSWORD"
LEGACY_AGENT_ACCESS_PASSWORD_ENV = "CACHELIKES_AGENT_PASSWORD"
AGENT_ACCESS_SESSION_KEY = "agent_access_granted"
PRIVATE_NETWORKS = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("fc00::/7"),
)


def resolve_agent_access_password() -> str:
    """Return the explicitly configured six-digit LAN password."""
    return (
        str(os.environ.get(AGENT_ACCESS_PASSWORD_ENV, "")).strip()
        or str(os.environ.get(LEGACY_AGENT_ACCESS_PASSWORD_ENV, "")).strip()
    )


def agent_access_password_is_configured() -> bool:
    """Return whether LAN access has one valid explicit password."""
    password = resolve_agent_access_password()
    return len(password) == 6 and password.isascii() and password.isdigit()


def validate_agent_access_password(presented_password: str | None) -> bool:
    """Compare one submitted password without exposing the configured value."""
    configured_password = resolve_agent_access_password()
    normalized_presented_password = str(presented_password or "").strip()
    return agent_access_password_is_configured() and secrets.compare_digest(
        normalized_presented_password,
        configured_password,
    )


def is_loopback_or_private_address(value: str | None) -> bool:
    """Return whether an address belongs to this host or a private network."""
    candidate = (value or "").strip().split("%", 1)[0]
    if not candidate:
        return False
    if candidate.lower() == "localhost":
        return True
    try:
        parsed_address = ip_address(candidate)
        return parsed_address.is_loopback or any(
            parsed_address in network for network in PRIVATE_NETWORKS
        )
    except ValueError:
        return False


def is_allowed_agent_network_request(remote_addr: str | None, host_name: str | None) -> bool:
    """Allow loopback and private-IP requests while rejecting public host rebinding."""
    return (
        is_loopback_or_private_address(remote_addr)
        and is_loopback_or_private_address(host_name)
    )
