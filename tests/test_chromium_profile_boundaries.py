"""Regression tests for Chromium profile identity and ownership boundaries.

Code version: v1.0.0-codex.1
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import browser_sessions, computer_use_agent
from app.core.config import CrawlConfig, load_saved_config, save_config, validate_chromium_profile_directory


@pytest.fixture
def clone_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Confine both clone creation and stale discovery to this test's root."""
    root = tmp_path / "clones"
    root.mkdir()
    original_factory = browser_sessions.tempfile.TemporaryDirectory

    def private_factory(**kwargs):
        return original_factory(dir=root, **kwargs)

    monkeypatch.setattr(browser_sessions.tempfile, "TemporaryDirectory", private_factory)
    monkeypatch.setattr(browser_sessions.tempfile, "gettempdir", lambda: str(root))
    monkeypatch.setattr(browser_sessions, "_ACTIVE_CHROMIUM_PROFILE_ROOTS", set())
    return root


def _descriptor(root: Path, profile: str = "Default", browser: str = "chrome"):
    return browser_sessions.BrowserDescriptor(
        browser_id=browser,
        label=browser.title(),
        icon_filename="",
        engine="chromium",
        user_data_dir=root,
        profile_directory=profile,
        channel="msedge" if browser == "edge" else "chrome",
    )


@pytest.mark.parametrize(
    "profile",
    (
        "", ".", "..", "../../outside", r"..\..\outside",
        "/outside", r"\outside", r"C:\Profiles\Default", "C:Default",
        r"\\server\share\Default", "Profile/Child", r"Profile\Child",
        "Default:stream", "NUL", "con.txt", "Profile.", "bad\x00name",
    ),
)
def test_invalid_profile_is_rejected_before_copy_or_temporary_creation(
    profile: str, tmp_path: Path, clone_base: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    copy = Mock()
    monkeypatch.setattr(browser_sessions.shutil, "copytree", copy)
    with pytest.raises(ValueError, match="one directory name"):
        browser_sessions.clone_browser_profile(_descriptor(source, profile))
    with pytest.raises(ValueError, match="one directory name"):
        CrawlConfig(chrome_profile_directory=profile)
    with pytest.raises(ValueError, match="one directory name"):
        browser_sessions.build_chromium_launch_args(_descriptor(source, profile))
    copy.assert_not_called()
    assert list(clone_base.iterdir()) == []


def test_valid_space_and_unicode_profile_round_trips_and_clones(tmp_path: Path, clone_base: Path) -> None:
    profile = "工作 Profile 2"
    source = tmp_path / "Chrome data 用户"
    (source / profile).mkdir(parents=True)
    (source / profile / "Preferences").write_text("synthetic preferences", encoding="utf-8")
    config = CrawlConfig(chrome_user_data_dir=source, chrome_profile_directory=profile)
    settings = tmp_path / "settings.json"
    save_config(config, settings)
    loaded = load_saved_config(settings)
    assert loaded.chrome_profile_directory == profile
    assert validate_chromium_profile_directory(profile) == profile
    descriptor = browser_sessions.browser_descriptors(loaded)["chrome"]
    target, temporary = browser_sessions.clone_browser_profile(descriptor)
    try:
        assert target.resolve().is_relative_to(clone_base.resolve())
        assert (target / profile / "Preferences").read_text(encoding="utf-8") == "synthetic preferences"
        assert browser_sessions.build_chromium_launch_args(descriptor)[0] == f"--profile-directory={profile}"
    finally:
        browser_sessions._cleanup_cloned_browser_profile(temporary)
    assert list(clone_base.iterdir()) == []
    assert (source / profile / "Preferences").read_text(encoding="utf-8") == "synthetic preferences"


def test_invalid_persisted_or_mutated_profile_fails_without_rewriting_settings(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"chrome_profile_directory": "../../outside"}), encoding="utf-8")
    previous = settings.read_bytes()
    with pytest.raises(ValueError, match="one directory name"):
        load_saved_config(settings)
    config = CrawlConfig()
    config.chrome_profile_directory = r"C:Default"
    with pytest.raises(ValueError, match="one directory name"):
        save_config(config, settings)
    with pytest.raises(ValueError, match="one directory name"):
        browser_sessions.browser_descriptors(config)
    assert settings.read_bytes() == previous


@pytest.mark.parametrize("link_kind", ("is_symlink", "is_junction", "resolved_escape"))
def test_source_profile_link_or_resolved_escape_is_rejected(
    link_kind: str, tmp_path: Path, clone_base: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (tmp_path / "source").resolve()
    profile = source / "Default"
    profile.mkdir(parents=True)
    if link_kind == "resolved_escape":
        original_resolve = Path.resolve
        monkeypatch.setattr(
            Path, "resolve",
            lambda path, *args, **kwargs: (
                tmp_path / "outside" if path == profile else original_resolve(path, *args, **kwargs)
            ),
        )
    else:
        original = getattr(Path, link_kind)
        monkeypatch.setattr(Path, link_kind, lambda path: path == profile or original(path))
    copy = Mock()
    monkeypatch.setattr(browser_sessions.shutil, "copytree", copy)
    with pytest.raises(ValueError, match="without a link or junction"):
        browser_sessions.clone_browser_profile(_descriptor(source))
    copy.assert_not_called()
    assert list(clone_base.iterdir()) == []


def test_target_containment_failure_cleans_only_the_allocated_clone(tmp_path: Path, clone_base: Path) -> None:
    source = tmp_path / "source"
    (source / "Default").mkdir(parents=True)
    destination = clone_base / "escaped"
    destination.mkdir()
    sentinel = destination / "sentinel.txt"
    sentinel.write_text("retain", encoding="utf-8")
    descriptor = browser_sessions.BrowserDescriptor(
        browser_id="chrome", label="../escaped/", icon_filename="", engine="chromium",
        user_data_dir=source, profile_directory="Default", channel="chrome",
    )
    with pytest.raises(ValueError, match="task-owned directory"):
        browser_sessions.clone_browser_profile(descriptor)
    assert sentinel.read_text(encoding="utf-8") == "retain"
    assert list(clone_base.iterdir()) == [destination]
    assert browser_sessions._ACTIVE_CHROMIUM_PROFILE_ROOTS == set()


@pytest.mark.parametrize("browser", ("edge", "chrome"))
def test_windows_task_uses_handoff_resolver_executable(
    browser: str, tmp_path: Path, clone_base: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    (source / "Profile 2").mkdir(parents=True)
    executable = tmp_path / "Installed 浏览器" / "browser.exe"
    resolver = Mock(return_value=str(executable))
    monkeypatch.setattr(computer_use_agent, "resolve_windows_browser_executable", resolver)
    monkeypatch.setattr(browser_sessions, "is_windows_host", lambda: True)
    monkeypatch.setattr(browser_sessions, "is_macos_host", lambda: False)
    launch = Mock(return_value=SimpleNamespace(close=lambda: None))
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    with browser_sessions.launch_chromium_context(playwright, _descriptor(source, "Profile 2", browser), False):
        pass
    resolver.assert_called_once_with(browser)
    assert launch.call_args.kwargs["executable_path"] == str(executable)
    assert launch.call_args.kwargs["channel"] == ("msedge" if browser == "edge" else "chrome")
    assert list(clone_base.iterdir()) == []


def test_missing_windows_executable_fails_before_copy_or_launch(
    tmp_path: Path, clone_base: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(browser_sessions, "is_windows_host", lambda: True)
    monkeypatch.setattr(computer_use_agent, "resolve_windows_browser_executable", lambda _browser: None)
    copy = Mock()
    monkeypatch.setattr(browser_sessions, "clone_browser_profile", copy)
    launch = Mock()
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    with pytest.raises(RuntimeError, match="could not be found"):
        browser_sessions.launch_chromium_context(playwright, _descriptor(source), False)
    copy.assert_not_called()
    launch.assert_not_called()
    assert list(clone_base.iterdir()) == []


def test_stale_clone_with_live_other_process_is_retained(tmp_path: Path, clone_base: Path) -> None:
    profile = clone_base / "cachelikes-chrome-live-other-process"
    profile.mkdir()
    sentinel = profile / "synthetic.txt"
    sentinel.write_text("retain", encoding="utf-8")
    ready = tmp_path / "owner-ready"
    child_code = (
        "from pathlib import Path; import sys; "
        "from app.core import browser_sessions; "
        "root=Path(sys.argv[1]); browser_sessions._ACTIVE_CHROMIUM_PROFILE_ROOTS.add(root); "
        "handle=(root/'synthetic.txt').open(); "
        "Path(sys.argv[2]).write_text('ready'); sys.stdin.read(); handle.close()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(profile), str(ready)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "The synthetic profile owner did not become ready."
        old = time.time() - 2 * browser_sessions.CHROMIUM_TEMP_PROFILE_STALE_AFTER_SECONDS
        os.utime(profile, (old, old))
        assert profile not in browser_sessions._ACTIVE_CHROMIUM_PROFILE_ROOTS
        assert browser_sessions._housekeep_stale_chromium_profiles(_descriptor(tmp_path)) == 0
        assert process.poll() is None
        assert sentinel.read_text(encoding="utf-8") == "retain"
    finally:
        try:
            process.communicate(input="", timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
    assert process.returncode == 0
    # An exited service alone still cannot prove that its browser family exited.
    assert browser_sessions._housekeep_stale_chromium_profiles(_descriptor(tmp_path)) == 0
    assert sentinel.read_text(encoding="utf-8") == "retain"
