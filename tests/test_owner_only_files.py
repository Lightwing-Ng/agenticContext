"""Owner-only secret-file boundary on POSIX and Windows, and its Tunnel consumers.

Code version: v1.0.0-claude.0
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import owner_only_files, tunnel_credentials
from app.core.computer_use_agent import ComputerUseSettings
from app.core.owner_only_files import (
    OwnerOnlyFileError,
    ensure_owner_only,
    owner_only_problem,
    write_owner_only_text,
)
from app.core.tunnel_credentials import (
    TunnelCredentials,
    default_tunnel_credentials_path,
    load_tunnel_credentials,
    save_tunnel_credentials,
)
from app.web.app import create_app

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit boundary")
windows_only = pytest.mark.skipif(os.name != "nt", reason="native Windows DACL boundary")

USER_SID = "S-1-5-21-1000-2000-3000-1001"
OWNER_ONLY_DACL = f"D:PAI(A;;FA;;;{USER_SID})"
INHERITED_DACL = f"D:AI(A;ID;FA;;;{USER_SID})(A;ID;FA;;;SY)(A;ID;FA;;;BA)"


@pytest.fixture
def permissive_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


@posix_only
def test_write_uses_mode_0600_even_with_a_permissive_umask(
    tmp_path: Path, permissive_umask
) -> None:
    target = tmp_path / "nested" / "secret"

    write_owner_only_text(target, "Bearer token")

    assert target.read_text() == "Bearer token"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert owner_only_problem(target) == ""
    assert not (target.parent / ".secret.tmp").exists()


@posix_only
def test_write_never_reuses_a_wider_leftover_or_destination(
    tmp_path: Path, permissive_umask
) -> None:
    target = tmp_path / "secret"
    target.write_text("old")
    target.chmod(0o644)
    leftover = tmp_path / ".secret.tmp"
    leftover.write_text("stale partial secret")
    leftover.chmod(0o666)

    write_owner_only_text(target, "new")

    assert target.read_text() == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not leftover.exists()


@posix_only
def test_write_replaces_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("unrelated")
    outside.chmod(0o644)
    target = tmp_path / "secret"
    target.symlink_to(outside)

    write_owner_only_text(target, "secret")

    assert not target.is_symlink()
    assert target.read_text() == "secret"
    assert outside.read_text() == "unrelated"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


@posix_only
def test_ensure_narrows_a_legacy_file_and_rejects_non_regular_paths(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.write_text("secret")
    legacy.chmod(0o644)
    assert owner_only_problem(legacy) == "its mode grants group or other access"

    ensure_owner_only(legacy)

    assert stat.S_IMODE(legacy.stat().st_mode) == 0o600
    link = tmp_path / "link"
    link.symlink_to(legacy)
    with pytest.raises(OwnerOnlyFileError):
        ensure_owner_only(link)
    assert owner_only_problem(link) == "it is not a regular file"
    assert owner_only_problem(tmp_path / "missing").startswith("it cannot be inspected")


def test_failed_verification_writes_no_secret_and_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "secret"
    write_owner_only_text(target, "old")
    real_problem = owner_only_problem

    def reject_temporary(path: Path) -> str:
        return "simulated wider boundary" if path.name.endswith(".tmp") else real_problem(path)

    monkeypatch.setattr(owner_only_files, "owner_only_problem", reject_temporary)

    with pytest.raises(OwnerOnlyFileError, match="simulated wider boundary"):
        write_owner_only_text(target, "new secret")

    assert target.read_text() == "old"
    assert list(tmp_path.iterdir()) == [target]


class FakeWindowsSecurity:
    """Model NTFS: a DACL belongs to the file object, so rename keeps it."""

    def __init__(self, *, fail_set: bool = False, applied_dacl: str = OWNER_ONLY_DACL) -> None:
        self.fail_set = fail_set
        self.applied_dacl = applied_dacl
        self.protected: set[int] = set()
        self.sizes_when_protected: list[int] = []

    def canonical_dacl(self, sddl: str) -> str:
        assert sddl == f"D:P(A;;FA;;;{USER_SID})"
        return sddl

    def set_protected_dacl(self, path: str, sddl: str) -> None:
        if self.fail_set:
            raise OwnerOnlyFileError("SetNamedSecurityInfoW failed with Windows error 5.")
        assert sddl == f"D:P(A;;FA;;;{USER_SID})"
        self.sizes_when_protected.append(os.stat(path).st_size)
        self.protected.add(os.stat(path).st_ino)

    def dacl_text(self, path: str) -> str:
        return self.applied_dacl if os.stat(path).st_ino in self.protected else INHERITED_DACL

    def current_user_sid(self) -> str:
        return USER_SID


@pytest.fixture
def simulated_windows(monkeypatch: pytest.MonkeyPatch):
    def install(security: FakeWindowsSecurity) -> FakeWindowsSecurity:
        monkeypatch.setattr(owner_only_files, "IS_WINDOWS", True)
        monkeypatch.setattr(owner_only_files, "_windows_security", lambda: security)
        owner_only_files._windows_owner_sddl.cache_clear()
        return security

    yield install
    owner_only_files._windows_owner_sddl.cache_clear()


def test_windows_protects_the_new_file_before_any_secret_byte(
    tmp_path: Path, simulated_windows
) -> None:
    security = simulated_windows(FakeWindowsSecurity())
    target = tmp_path / "secret"

    write_owner_only_text(target, "sk-proj-secret")

    assert security.sizes_when_protected == [0]
    assert target.read_text() == "sk-proj-secret"
    assert owner_only_problem(target) == ""


def test_windows_rejects_inherited_or_shared_dacls(tmp_path: Path, simulated_windows) -> None:
    simulated_windows(FakeWindowsSecurity())
    plain = tmp_path / "plain"
    plain.write_text("secret")
    assert owner_only_problem(plain) == "its Windows DACL still inherits parent permissions"

    simulated_windows(
        FakeWindowsSecurity(applied_dacl=f"D:P(A;;FA;;;{USER_SID})(A;;FR;;;BU)")
    )
    with pytest.raises(OwnerOnlyFileError, match="beyond the current user"):
        write_owner_only_text(tmp_path / "shared", "secret")
    assert not (tmp_path / "shared").exists()
    assert not (tmp_path / ".shared.tmp").exists()


def test_windows_acl_failure_keeps_the_previous_secret(
    tmp_path: Path, simulated_windows
) -> None:
    target = tmp_path / "secret"
    target.write_text("previous")
    simulated_windows(FakeWindowsSecurity(fail_set=True))

    with pytest.raises(OwnerOnlyFileError, match="Windows error 5"):
        write_owner_only_text(target, "replacement")

    assert target.read_text() == "previous"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["secret"]


def test_windows_ensure_repairs_a_legacy_inherited_file(
    tmp_path: Path, simulated_windows
) -> None:
    security = simulated_windows(FakeWindowsSecurity())
    legacy = tmp_path / "legacy"
    legacy.write_text("secret")

    ensure_owner_only(legacy)

    assert owner_only_problem(legacy) == ""
    assert security.sizes_when_protected == [len("secret")]


@windows_only
def test_native_windows_dacl_is_protected_and_owner_only(tmp_path: Path) -> None:
    security = owner_only_files._windows_security()
    sid = security.current_user_sid()
    assert sid.startswith("S-1-")
    plain = tmp_path / "plain"
    plain.write_text("not secret")
    target = tmp_path / "secret"

    write_owner_only_text(target, "sk-proj-secret")

    assert target.read_text(encoding="utf-8") == "sk-proj-secret"
    assert owner_only_problem(target) == ""
    flags, aces = owner_only_files._parse_dacl(security.dacl_text(str(target)))
    assert "P" in flags
    expected = owner_only_files._parse_dacl(security.canonical_dacl(f"D:P(A;;FA;;;{sid})"))
    assert aces == expected[1]
    # A file created normally in the same folder still inherits, so the helper did the work.
    assert owner_only_problem(plain) != ""
    ensure_owner_only(plain)
    assert owner_only_problem(plain) == ""


@pytest.fixture
def credentials_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTIC_CONTEXT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    return default_tunnel_credentials_path()


@posix_only
def test_legacy_tunnel_credentials_are_narrowed_on_load(credentials_path: Path) -> None:
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    credentials_path.write_text(json.dumps({"tunnel_id": "tunnel_abc", "api_key": "sk-proj-x"}))
    credentials_path.chmod(0o644)

    assert load_tunnel_credentials() == TunnelCredentials("tunnel_abc", "sk-proj-x")
    assert stat.S_IMODE(credentials_path.stat().st_mode) == 0o600


def test_unnarrowable_credentials_still_load_and_warn(
    credentials_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    save_tunnel_credentials(TunnelCredentials("tunnel_abc", "sk-proj-secret-ABCD"))
    monkeypatch.setattr(tunnel_credentials, "owner_only_problem", lambda _path: "simulated")

    def refuse(_path: Path) -> None:
        raise OwnerOnlyFileError("simulated ACL refusal")

    monkeypatch.setattr(tunnel_credentials, "ensure_owner_only", refuse)
    with caplog.at_level(logging.WARNING, logger=tunnel_credentials.__name__):
        loaded = load_tunnel_credentials()

    assert loaded == TunnelCredentials("tunnel_abc", "sk-proj-secret-ABCD")
    assert "simulated ACL refusal" in caplog.text
    assert "sk-proj-secret-ABCD" not in caplog.text


def test_credentials_route_refuses_to_save_without_the_boundary(
    credentials_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = TunnelCredentials("tunnel_" + "a" * 32, "sk-proj-saved-secret")
    save_tunnel_credentials(saved)
    with patch(
        "app.core.computer_use_agent.load_computer_use_settings",
        return_value=ComputerUseSettings(browser="edge", platform="chatgpt"),
    ):
        application = create_app()
    application.config.update(TESTING=True)
    runtime = application.extensions["tunnel_runtime"]
    restarts: list[float] = []
    monkeypatch.setattr(runtime, "request_restart", lambda delay=0.0: restarts.append(delay))

    def refuse(*_args, **_kwargs) -> None:
        raise OwnerOnlyFileError("simulated ACL refusal")

    monkeypatch.setattr("app.web.tunnel_routes.save_tunnel_credentials", refuse)
    with application.test_client() as client:
        response = client.post(
            "/api/agent/tunnel/credentials",
            json={"tunnel_id": "tunnel_" + "b" * 32, "api_key": "sk-proj-new-secret"},
        )

    body = response.get_data(as_text=True)
    assert response.status_code == 500
    assert response.get_json() == {
        "error": "Tunnel credentials could not be saved with owner-only access."
    }
    assert "sk-proj-new-secret" not in body
    assert load_tunnel_credentials() == saved
    assert restarts == []
