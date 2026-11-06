"""Owner-only secret-file boundary on POSIX and Windows, and its Tunnel consumers.

Code version: v1.2.1-codex.0
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
    ensure_owner_only_directory,
    owner_only_directory_problem,
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


def test_owner_only_directory_uses_the_native_boundary(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir()

    ensure_owner_only_directory(directory)

    assert owner_only_directory_problem(directory) == ""
    if os.name == "posix":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    else:
        security = owner_only_files._windows_security()
        flags, aces = owner_only_files._parse_dacl(security.dacl_text(str(directory)))
        sid = security.current_user_sid()
        expected = owner_only_files._parse_dacl(security.canonical_dacl(f"D:P(A;OICI;FA;;;{sid})"))
        assert "P" in flags
        assert aces == expected[1]


def test_private_parent_keeps_existing_and_new_children_accessible(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    locks = runtime / "compute-job-locks"
    locks.mkdir(parents=True)
    existing = locks / "existing.lock"
    existing.write_bytes(b"existing synthetic lock")

    ensure_owner_only_directory(runtime)
    created = runtime / "new-child"
    created.mkdir()
    added = created / "new.lock"
    added.write_bytes(b"new synthetic lock")
    ensure_owner_only_directory(runtime)

    assert set(runtime.iterdir()) == {locks, created}
    assert list(locks.iterdir()) == [existing]
    assert list(created.iterdir()) == [added]
    assert existing.read_bytes() == b"existing synthetic lock"
    assert added.read_bytes() == b"new synthetic lock"
    if os.name == "nt":
        security = owner_only_files._windows_security()
        sid = security.current_user_sid()
        expected = owner_only_files._parse_dacl(security.canonical_dacl(f"D:P(A;;FA;;;{sid})"))
        expected_kind, _flags, expected_rights, expected_object, expected_inherited, expected_sid = expected[1][0][1:-1].split(";")
        for path in (locks, created, existing, added):
            _flags, aces = owner_only_files._parse_dacl(security.dacl_text(str(path)))
            assert len(aces) == 1
            kind, flags, rights, object_type, inherited_type, principal = aces[0][1:-1].split(";")
            assert (kind, rights, object_type, inherited_type, principal) == (
                expected_kind, expected_rights, expected_object, expected_inherited, expected_sid
            )
            assert {flags[index:index + 2] for index in range(0, len(flags), 2)} == (
                {"OI", "CI", "ID"} if path.is_dir() else {"ID"}
            )
        # Individual private files still require their own protected, exact DACL.
        assert owner_only_problem(added) == "its Windows DACL still inherits parent permissions"
    ensure_owner_only(added)
    ensure_owner_only_directory(created)
    assert owner_only_problem(added) == ""
    assert owner_only_directory_problem(created) == ""


def test_directory_boundary_rejects_a_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"preserved")
    with pytest.raises(OwnerOnlyFileError, match="not a regular directory"):
        ensure_owner_only_directory(path)
    assert path.read_bytes() == b"preserved"


def test_file_boundary_rejects_multiple_hard_links(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"preserved")
    linked = tmp_path / "linked"
    os.link(path, linked)
    before = path.stat()
    with pytest.raises(OwnerOnlyFileError, match="multiple hard links"):
        ensure_owner_only(path)
    assert owner_only_problem(path) == "it has multiple hard links"
    assert linked.read_bytes() == b"preserved"
    assert path.stat().st_mode == before.st_mode


def test_mutable_canonical_aliases_keep_the_native_owner_only_boundary(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    path.write_bytes(b"preserved canonical bytes")
    if os.name == "posix":
        path.chmod(0o644)
    linked = tmp_path / "initial-publication.tmp"
    os.link(path, linked)

    ensure_owner_only(path, allow_hard_links=True)

    assert path.samefile(linked)
    assert path.read_bytes() == linked.read_bytes() == b"preserved canonical bytes"
    assert owner_only_problem(path, allow_hard_links=True) == ""
    assert owner_only_problem(linked, allow_hard_links=True) == ""
    assert owner_only_problem(path) == "it has multiple hard links"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("allow_hard_links", [False, True])
def test_alias_option_never_allows_a_nonregular_file(tmp_path: Path, allow_hard_links: bool) -> None:
    with pytest.raises(OwnerOnlyFileError, match="not a regular file"):
        ensure_owner_only(tmp_path, allow_hard_links=allow_hard_links)
    assert owner_only_problem(tmp_path, allow_hard_links=allow_hard_links) == "it is not a regular file"


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

    def __init__(self, *, fail_set: bool = False, applied_dacl: str | None = None) -> None:
        self.fail_set = fail_set
        self.applied_dacl = applied_dacl
        self.protected: set[int] = set()
        self.dacls: dict[int, str] = {}
        self.sizes_when_protected: list[int] = []

    def canonical_dacl(self, sddl: str) -> str:
        assert sddl in {f"D:P(A;;FA;;;{USER_SID})", f"D:P(A;OICI;FA;;;{USER_SID})"}
        return sddl

    def set_protected_dacl(self, path: str, sddl: str) -> None:
        if self.fail_set:
            raise OwnerOnlyFileError("SetNamedSecurityInfoW failed with Windows error 5.")
        assert sddl in {f"D:P(A;;FA;;;{USER_SID})", f"D:P(A;OICI;FA;;;{USER_SID})"}
        self.sizes_when_protected.append(os.stat(path).st_size)
        self.protected.add(os.stat(path).st_ino)
        self.dacls[os.stat(path).st_ino] = self.applied_dacl or sddl

    def dacl_text(self, path: str) -> str:
        current = Path(path)
        if current.stat().st_ino in self.dacls:
            return self.dacls[current.stat().st_ino]
        for parent in current.parents:
            policy = self.dacls.get(parent.stat().st_ino)
            if policy is not None:
                inherit = "CI" if current.is_dir() else "OI"
                if inherit not in policy:
                    return "D:"
                flags = "OICIID" if current.is_dir() else "ID"
                return f"D:AI(A;{flags};FA;;;{USER_SID})"
        return INHERITED_DACL

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


def test_windows_directory_policy_propagates_only_current_user_access(
    tmp_path: Path, simulated_windows
) -> None:
    security = simulated_windows(FakeWindowsSecurity())
    runtime = tmp_path / "runtime"
    locks = runtime / "compute-job-locks"
    locks.mkdir(parents=True)
    existing = locks / "existing.lock"
    existing.write_bytes(b"existing synthetic lock")

    ensure_owner_only_directory(runtime)
    created = runtime / "new-child"
    created.mkdir()
    added = created / "new.lock"
    added.write_bytes(b"new synthetic lock")
    ensure_owner_only_directory(runtime)

    assert security.dacl_text(str(runtime)) == f"D:P(A;OICI;FA;;;{USER_SID})"
    for path in (locks, created):
        assert security.dacl_text(str(path)) == f"D:AI(A;OICIID;FA;;;{USER_SID})"
        assert list(path.iterdir())
    for path in (existing, added):
        assert security.dacl_text(str(path)) == f"D:AI(A;ID;FA;;;{USER_SID})"
        assert path.read_bytes()
    ensure_owner_only(added)
    assert security.dacl_text(str(added)) == f"D:P(A;;FA;;;{USER_SID})"
    assert owner_only_problem(added) == ""


def test_windows_directory_refuses_a_noninheritable_file_policy(
    tmp_path: Path, simulated_windows
) -> None:
    simulated_windows(FakeWindowsSecurity(applied_dacl=OWNER_ONLY_DACL))
    directory = tmp_path / "private"
    directory.mkdir()

    with pytest.raises(OwnerOnlyFileError):
        ensure_owner_only_directory(directory)

    assert owner_only_directory_problem(directory)


def test_windows_mutable_aliases_are_narrowed_and_verified_as_one_file(
    tmp_path: Path, simulated_windows
) -> None:
    security = simulated_windows(FakeWindowsSecurity())
    path = tmp_path / "history.sqlite3"
    path.write_bytes(b"preserved canonical bytes")
    linked = tmp_path / "initial-publication.tmp"
    os.link(path, linked)
    assert owner_only_problem(path, allow_hard_links=True) == "its Windows DACL still inherits parent permissions"

    ensure_owner_only(path, allow_hard_links=True)

    assert security.sizes_when_protected == [len(b"preserved canonical bytes")]
    assert owner_only_problem(path, allow_hard_links=True) == ""
    assert owner_only_problem(linked, allow_hard_links=True) == ""
    assert path.samefile(linked)
    assert linked.read_bytes() == b"preserved canonical bytes"
    with pytest.raises(OwnerOnlyFileError, match="multiple hard links"):
        ensure_owner_only(path)


@pytest.mark.parametrize("fail_set", [True, False])
def test_windows_mutable_alias_option_never_bypasses_dacl_failure(
    tmp_path: Path, simulated_windows, fail_set: bool
) -> None:
    simulated_windows(FakeWindowsSecurity(fail_set=fail_set, applied_dacl=INHERITED_DACL))
    path = tmp_path / "history.sqlite3"
    path.write_bytes(b"preserved canonical bytes")
    linked = tmp_path / "initial-publication.tmp"
    os.link(path, linked)

    with pytest.raises(OwnerOnlyFileError):
        ensure_owner_only(path, allow_hard_links=True)

    assert owner_only_problem(path, allow_hard_links=True)
    assert path.read_bytes() == linked.read_bytes() == b"preserved canonical bytes"


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


@pytest.mark.parametrize("fail_set", [True, False])
def test_windows_directory_failure_is_not_reported_private(
    tmp_path, simulated_windows, fail_set,
) -> None:
    directory = tmp_path / "private"
    directory.mkdir()
    simulated_windows(FakeWindowsSecurity(fail_set=fail_set, applied_dacl=INHERITED_DACL))

    with pytest.raises(OwnerOnlyFileError):
        ensure_owner_only_directory(directory)

    assert owner_only_directory_problem(directory)
    assert list(directory.iterdir()) == []


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
