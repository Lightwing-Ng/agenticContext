"""Security and lifecycle tests for local browser acceptance.

Code version: v1.0.0-codex.1
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import socket

import pytest

from app.core.agent.browser_acceptance import (
    BrowserAcceptanceRequest,
    load_project_protected_ports,
    run_browser_acceptance,
    validate_browser_acceptance_target,
    validate_browser_request_url,
)
from app.core.computer_use_agent import (
    ComputerUseSettings,
    WorkspaceController,
    _activity_detail,
    _process_group_options,
    _stop_process,
)


def _request(tmp_path: Path, root: Path, **changes: object) -> BrowserAcceptanceRequest:
    values = {
        "root": root,
        "target": "/",
        "port": 0,
        "expected_text": ("Ready",),
        "expected_selectors": ("main",),
        "protected_ports": frozenset({8666}),
        "timeout_seconds": 20.0,
        "artifact_root": tmp_path / "runtime",
    }
    values.update(changes)
    return BrowserAcceptanceRequest(**values)


def test_browser_acceptance_activity_detail_is_specific_and_bounded() -> None:
    assert _activity_detail(
        {
            "action": "browser_acceptance",
            "root": "public",
            "target": "/dashboard",
        }
    ) == "public → /dashboard (desktop + narrow Chromium)"


def test_workspace_controller_executes_registered_browser_acceptance(tmp_path: Path) -> None:
    pytest.importorskip("playwright.sync_api")
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "index.html").write_text(
        "<!doctype html><html><body><main>Ready</main></body></html>",
        encoding="utf-8",
    )
    controller = WorkspaceController(
        workspace,
        ComputerUseSettings(workspace_path=str(workspace), command_timeout_seconds=20),
        lambda: False,
        compute_job_runtime_root=tmp_path / "runtime",
    )
    result = controller.execute(
        {
            "action": "browser_acceptance",
            "root": ".",
            "target": "/",
            "expected_text": ["Ready"],
            "expected_selectors": ["main"],
        }
    )
    assert result["ok"] is True
    assert result["action"] == "browser_acceptance"
    assert result["preview"]["port"] != 8666
    assert result["verification_current"] is True
    assert "browser_acceptance" in controller.state.successful_checks


def test_project_protected_ports_are_bounded_and_fail_closed(tmp_path: Path) -> None:
    config = tmp_path / ".agenticContext-browser-acceptance.json"
    config.write_text(
        '{"schema_version":1,"protected_ports":[4321,8765,4321]}',
        encoding="utf-8",
    )
    assert load_project_protected_ports(tmp_path) == frozenset({4321, 8765})
    config.write_text(
        '{"schema_version":1,"protected_ports":[0]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid port"):
        load_project_protected_ports(tmp_path)


def test_browser_acceptance_target_rejects_non_owned_or_credentialed_urls() -> None:
    assert validate_browser_acceptance_target("/ready?mode=test", 49152).startswith(
        "http://127.0.0.1:49152/ready"
    )
    for target in (
        "https://example.com/",
        "file:///tmp/index.html",
        "http://user:pass@127.0.0.1:49152/",
        "http://localhost:49153/",
        "//example.com/path",
    ):
        with pytest.raises(ValueError):
            validate_browser_acceptance_target(target, 49152)


def test_browser_request_policy_blocks_external_hosts_and_unowned_ports() -> None:
    assert validate_browser_request_url("http://127.0.0.1:49152/app.js", 49152)
    assert validate_browser_request_url("http://localhost:49152/app.js", 49152)
    assert validate_browser_request_url("data:text/plain,ok", 49152)
    assert not validate_browser_request_url("https://example.com/app.js", 49152)
    assert not validate_browser_request_url("http://127.0.0.1:49153/app.js", 49152)
    assert not validate_browser_request_url("file:///tmp/secret", 49152)
    assert not validate_browser_request_url(
        "http://user:pass@127.0.0.1:49152/app.js",
        49152,
    )


def test_browser_acceptance_rejects_protected_application_port(tmp_path: Path) -> None:
    root = tmp_path / "site"
    root.mkdir()
    with pytest.raises(ValueError, match="protected"):
        run_browser_acceptance(
            _request(tmp_path, root, port=8666),
            playwright_context=lambda: pytest.fail("browser must not launch"),
            process_group_options=_process_group_options,
            stop_process=_stop_process,
            process_changed=lambda _process: None,
            should_stop=lambda: False,
        )


def test_browser_acceptance_fails_closed_on_occupied_port_and_cleans_process(
    tmp_path: Path,
) -> None:
    root = tmp_path / "site"
    root.mkdir()
    observed = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen(1)
        port = int(owner.getsockname()[1])
        with pytest.raises(RuntimeError, match="unavailable"):
            run_browser_acceptance(
                _request(tmp_path, root, port=port),
                playwright_context=lambda: pytest.fail("browser must not launch"),
                process_group_options=_process_group_options,
                stop_process=_stop_process,
                process_changed=observed.append,
                should_stop=lambda: False,
            )
    processes = [process for process in observed if process is not None]
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert observed[-1] is None


def test_browser_acceptance_process_registration_exception_cleans_owned_preview(tmp_path: Path) -> None:
    root = tmp_path / "site"
    root.mkdir()
    observed = []

    def fail_registration(process: object) -> None:
        observed.append(process)
        raise RuntimeError("synthetic controller registration failure")

    with pytest.raises(RuntimeError, match="synthetic controller registration failure"):
        run_browser_acceptance(
            _request(tmp_path, root),
            playwright_context=lambda: pytest.fail("browser must not launch"),
            process_group_options=_process_group_options,
            stop_process=_stop_process,
            process_changed=fail_registration,
            should_stop=lambda: False,
        )
    assert len(observed) == 1
    assert observed[0].poll() is not None


def test_browser_acceptance_controller_exception_cleans_owned_preview(tmp_path: Path) -> None:
    root = tmp_path / "site"
    root.mkdir()
    observed = []

    @contextmanager
    def broken_playwright():
        raise RuntimeError("synthetic playwright failure")
        yield

    with pytest.raises(RuntimeError, match="synthetic playwright failure"):
        run_browser_acceptance(
            _request(tmp_path, root),
            playwright_context=broken_playwright,
            process_group_options=_process_group_options,
            stop_process=_stop_process,
            process_changed=observed.append,
            should_stop=lambda: False,
        )
    processes = [process for process in observed if process is not None]
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert observed[-1] is None


def test_browser_acceptance_stop_cleans_owned_preview(tmp_path: Path) -> None:
    root = tmp_path / "site"
    root.mkdir()
    observed = []
    with pytest.raises(RuntimeError, match="Stop requested"):
        run_browser_acceptance(
            _request(tmp_path, root),
            playwright_context=lambda: pytest.fail("browser must not launch"),
            process_group_options=_process_group_options,
            stop_process=_stop_process,
            process_changed=observed.append,
            should_stop=lambda: True,
        )
    processes = [process for process in observed if process is not None]
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert observed[-1] is None


def test_browser_acceptance_timeout_cleans_owned_preview(tmp_path: Path) -> None:
    playwright_sync = pytest.importorskip("playwright.sync_api")
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text("<main>Ready</main>", encoding="utf-8")
    observed = []
    with pytest.raises(RuntimeError, match="timed out"):
        run_browser_acceptance(
            _request(tmp_path, root, timeout_seconds=0.001),
            playwright_context=playwright_sync.sync_playwright,
            process_group_options=_process_group_options,
            stop_process=_stop_process,
            process_changed=observed.append,
            should_stop=lambda: False,
        )
    processes = [process for process in observed if process is not None]
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert observed[-1] is None


def test_browser_acceptance_real_chromium_desktop_and_narrow_cleanup(tmp_path: Path) -> None:
    playwright_sync = pytest.importorskip("playwright.sync_api")
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><html><body><main>Ready</main></body></html>",
        encoding="utf-8",
    )
    observed = []
    result = run_browser_acceptance(
        _request(tmp_path, root),
        playwright_context=playwright_sync.sync_playwright,
        process_group_options=_process_group_options,
        stop_process=_stop_process,
        process_changed=observed.append,
        should_stop=lambda: False,
    )
    assert result["ok"] is True
    assert result["preview"]["host"] == "127.0.0.1"
    assert result["preview"]["port"] != 8666
    assert [entry["viewport"]["name"] for entry in result["viewports"]] == [
        "desktop",
        "narrow",
    ]
    assert all(entry["ok"] for entry in result["viewports"])
    assert all(entry["screenshot_path"] for entry in result["viewports"])
    assert all(entry["trace_path"] for entry in result["viewports"])
    processes = [process for process in observed if process is not None]
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert observed[-1] is None


def test_browser_acceptance_assertion_failure_is_bounded_and_cleans_preview(tmp_path: Path) -> None:
    playwright_sync = pytest.importorskip("playwright.sync_api")
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><html><body><main>Ready</main></body></html>",
        encoding="utf-8",
    )
    observed = []
    result = run_browser_acceptance(
        _request(tmp_path, root, expected_text=("Not present",)),
        playwright_context=playwright_sync.sync_playwright,
        process_group_options=_process_group_options,
        stop_process=_stop_process,
        process_changed=observed.append,
        should_stop=lambda: False,
    )
    assert result["ok"] is False
    assert "page_content" not in result
    assert all("page_content" not in entry for entry in result["viewports"])
    assert all(len(entry["console_errors"]) <= 20 for entry in result["viewports"])
    processes = [process for process in observed if process is not None]
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert observed[-1] is None
