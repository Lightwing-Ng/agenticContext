"""Behavioral tests for private, persistent LAN launch settings.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TextIO

import pytest

from app.core import config
from app.core.agent_access_security import (
    agent_access_password_is_configured,
    resolve_agent_access_password,
    validate_agent_access_password,
)


@pytest.fixture(autouse=True)
def private_network_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """Keep every launch-setting read away from the host's private configuration."""
    for variable_name in (
        "AGENTIC_CONTEXT_HOST",
        "CACHELIKES_HOST",
        "AGENTIC_CONTEXT_AGENT_PASSWORD",
        "CACHELIKES_AGENT_PASSWORD",
    ):
        monkeypatch.delenv(variable_name, raising=False)
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "default_settings_path", lambda: settings_path)
    monkeypatch.setenv("AGENTIC_CONTEXT_SETTINGS_PATH", str(settings_path))
    return tmp_path / "network-access.json"


def write_network_config(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def assert_network_configuration_is_closed() -> None:
    assert config.load_network_access_config() == {}
    assert config.resolve_listen_host() == "127.0.0.1"
    assert resolve_agent_access_password() == ""
    assert not agent_access_password_is_configured()
    assert not validate_agent_access_password("246810")


def test_network_access_file_follows_the_current_private_settings_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    private_network_config: Path,
) -> None:
    assert config.network_access_config_path() == private_network_config

    alternate_settings = tmp_path / "alternate" / "crawler-settings.json"
    monkeypatch.setattr(config, "default_settings_path", lambda: alternate_settings)

    assert config.network_access_config_path() == alternate_settings.with_name(
        "network-access.json"
    )


def test_missing_network_access_file_keeps_loopback_and_disables_remote_unlock() -> None:
    assert_network_configuration_is_closed()


@pytest.mark.parametrize("host", ("127.0.0.1", "0.0.0.0"))
def test_complete_private_settings_supply_both_launch_host_and_pin(
    private_network_config: Path,
    host: str,
) -> None:
    payload = {"host": host, "agent_password": "246810"}
    write_network_config(private_network_config, payload)

    assert config.load_network_access_config() == payload
    assert config.resolve_listen_host() == host
    assert resolve_agent_access_password() == "246810"
    assert agent_access_password_is_configured()
    assert validate_agent_access_password("246810")
    assert not validate_agent_access_password("135790")


@pytest.mark.parametrize(
    "payload",
    (
        [],
        None,
        {"host": "0.0.0.0"},
        {"agent_password": "246810"},
        {"host": "192.168.124.10", "agent_password": "246810"},
        {"host": " 0.0.0.0 ", "agent_password": "246810"},
        {"host": None, "agent_password": "246810"},
        {"host": "0.0.0.0", "agent_password": 246810},
        {"host": "0.0.0.0", "agent_password": "24681"},
        {"host": "0.0.0.0", "agent_password": "2468100"},
        {"host": "0.0.0.0", "agent_password": "abcdef"},
        {"host": "0.0.0.0", "agent_password": "２４６８１０"},
        {"host": "0.0.0.0", "agent_password": " 246810 "},
    ),
)
def test_incomplete_or_invalid_private_settings_fail_closed_as_one_unit(
    private_network_config: Path,
    payload: object,
) -> None:
    write_network_config(private_network_config, payload)

    assert_network_configuration_is_closed()


@pytest.mark.parametrize("raw", (b'{"host":', b"\xff\xfe"))
def test_malformed_private_settings_fail_closed(
    private_network_config: Path,
    raw: bytes,
) -> None:
    private_network_config.write_bytes(raw)

    assert_network_configuration_is_closed()


@pytest.mark.parametrize("length", (4_096, 4_097))
def test_private_settings_enforce_the_read_limit(
    private_network_config: Path,
    length: int,
) -> None:
    raw = json.dumps({"host": "0.0.0.0", "agent_password": "246810"})
    private_network_config.write_text(raw.ljust(length), encoding="utf-8")

    if length == 4_096:
        assert config.resolve_listen_host() == "0.0.0.0"
        assert validate_agent_access_password("246810")
    else:
        assert_network_configuration_is_closed()


def test_symlinked_private_settings_fail_closed(
    private_network_config: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "other-network-access.json"
    write_network_config(target, {"host": "0.0.0.0", "agent_password": "246810"})
    try:
        private_network_config.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    assert_network_configuration_is_closed()


def test_unreadable_private_settings_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    private_network_config: Path,
) -> None:
    write_network_config(
        private_network_config,
        {"host": "0.0.0.0", "agent_password": "246810"},
    )
    original_open = Path.open

    def deny_private_config_read(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> TextIO:
        if path == private_network_config:
            raise PermissionError("Private launch configuration is unreadable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_private_config_read)

    assert_network_configuration_is_closed()


@pytest.mark.parametrize(
    ("current", "legacy", "expected_host", "expected_pin"),
    (
        (True, True, "127.0.0.1", "135790"),
        (False, True, "192.168.124.10", "123456"),
        (False, False, "0.0.0.0", "246810"),
    ),
)
def test_current_then_legacy_environment_overrides_private_settings(
    monkeypatch: pytest.MonkeyPatch,
    private_network_config: Path,
    current: bool,
    legacy: bool,
    expected_host: str,
    expected_pin: str,
) -> None:
    write_network_config(
        private_network_config,
        {"host": "0.0.0.0", "agent_password": "246810"},
    )
    monkeypatch.setenv("AGENTIC_CONTEXT_HOST", " 127.0.0.1 " if current else " ")
    monkeypatch.setenv("CACHELIKES_HOST", " 192.168.124.10 " if legacy else " ")
    monkeypatch.setenv("AGENTIC_CONTEXT_AGENT_PASSWORD", " 135790 " if current else " ")
    monkeypatch.setenv("CACHELIKES_AGENT_PASSWORD", " 123456 " if legacy else " ")

    assert config.resolve_listen_host() == expected_host
    assert resolve_agent_access_password() == expected_pin
    assert validate_agent_access_password(expected_pin)


@pytest.mark.parametrize(
    "variable_name",
    ("AGENTIC_CONTEXT_AGENT_PASSWORD", "CACHELIKES_AGENT_PASSWORD"),
)
def test_invalid_explicit_environment_pin_cannot_fall_back_to_private_pin(
    monkeypatch: pytest.MonkeyPatch,
    private_network_config: Path,
    variable_name: str,
) -> None:
    write_network_config(
        private_network_config,
        {"host": "0.0.0.0", "agent_password": "246810"},
    )
    if variable_name == "AGENTIC_CONTEXT_AGENT_PASSWORD":
        monkeypatch.setenv("CACHELIKES_AGENT_PASSWORD", "123456")
    monkeypatch.setenv(variable_name, "invalid")

    assert resolve_agent_access_password() == "invalid"
    assert not agent_access_password_is_configured()
    assert not validate_agent_access_password("246810")
    assert not validate_agent_access_password("invalid")


def test_a_fresh_launch_loads_persisted_host_and_pin(
    private_network_config: Path,
) -> None:
    write_network_config(
        private_network_config,
        {"host": "0.0.0.0", "agent_password": "246810"},
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.core.config import DEFAULT_HOST; "
            "from app.core.agent_access_security import validate_agent_access_password; "
            "assert DEFAULT_HOST == '0.0.0.0'; "
            "assert validate_agent_access_password('246810')",
        ],
        cwd=config.PROJECT_ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
