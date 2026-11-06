"""Real-renderer regressions for bounded Jury failure diagnostics.

Code version: v1.0.1-codex.0
"""

import json
from pathlib import Path
import time

import pytest
from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError

from tests import test_sidebar_e2e as fixtures


disposable_browser = fixtures.disposable_browser


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("fault", ("resource", "loop"))
def test_original_timeout_survives_diagnostics_and_context_closes(
    disposable_browser, tmp_path: Path, monkeypatch, fault: str
):
    context = disposable_browser.new_context()
    page = context.new_page()
    original_goto = Page.goto
    loop_started = []
    page.on(
        "console",
        lambda message: loop_started.append(True)
        if message.text == "jury-diagnostic-loop-start"
        else None,
    )

    def respond(route):
        if route.request.url.endswith("/probe"):
            route.fulfill(
                body='<script src="/static/diagnostic-fault.js"></script>',
                content_type="text/html",
            )
        elif route.request.url.endswith("/static/diagnostic-fault.js"):
            if fault == "loop":
                route.fulfill(
                    body='console.log("jury-diagnostic-loop-start"); while (true) {}',
                    content_type="text/javascript",
                )
        else:
            route.fulfill(status=404, body="")

    # Every fixture request is intercepted; no server or account is contacted.
    context.route("http://127.0.0.1:49111/**", respond)
    original_error = None
    diagnostic_seconds = None
    try:
        with pytest.raises(PlaywrightTimeoutError) as raised:
            try:
                # This short budget belongs only to the new injected-fault test.
                page.goto("http://127.0.0.1:49111/probe", timeout=1_000)
            except PlaywrightTimeoutError as error:
                original_error = error
                if fault == "loop":
                    assert loop_started, "The renderer must enter the actual infinite loop."
                from tests import jury_navigation_diagnostics as diagnostics
                from tests.test_jury_e2e import _record_jury_navigation_failure

                output = tmp_path / "quality-shard-3"
                monkeypatch.setattr(diagnostics, "OUTPUT", output / "failure.jsonl")
                monkeypatch.setattr(diagnostics, "THREADS", output / "threads.log")
                began = time.monotonic()
                _record_jury_navigation_failure(page, error)
                diagnostic_seconds = time.monotonic() - began
                raise
            finally:
                context.close()

        assert raised.value is original_error
        assert Page.goto is original_goto
        assert context not in disposable_browser.contexts
        assert diagnostic_seconds < 8
        records = [
            json.loads(line)
            for line in (output / "failure.jsonl").read_text().splitlines()
        ]
        assert records[0]["kind"] == "navigation_failure"
        assert records[-1]["kind"] == "capture_complete"
        assert (output / "threads.log").is_file()
        if fault == "resource":
            snapshot = next(row["snapshot"] for row in records if row["kind"] == "runtime")
            assert any(
                row["path"] == "/static/diagnostic-fault.js"
                for row in snapshot["pending_inferred"]
            )
        else:
            assert any(
                (
                    row["kind"] == "cdp_deadline" and row["method"] == "Runtime.evaluate"
                ) or (
                    row["kind"] == "runtime_unresponsive"
                    and row["native_execution_terminated"] is True
                )
                for row in records
            )
            assert any(row["kind"] == "runtime_unresponsive" for row in records)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_native_renderer_execution_timeout_has_explicit_evidence(disposable_browser):
    import asyncio

    from tests.jury_navigation_diagnostics import native_execution_terminated

    context = disposable_browser.new_context()
    page = context.new_page()
    loop_started = []
    page.on(
        "console",
        lambda message: loop_started.append(True)
        if message.text == "jury-native-execution-loop-start"
        else None,
    )
    try:
        channel = context.new_cdp_session(page)
        began = time.monotonic()
        with pytest.raises(PlaywrightError) as raised:
            # The real engine budget, not an outer transport deadline, must end
            # this evaluation and expose CDP's explicit termination evidence.
            # The safety deadline prevents a broken engine from hanging this
            # negative test; its Python TimeoutError still fails the contract.
            channel._sync(
                asyncio.wait_for(
                    channel._impl_obj.send("Runtime.evaluate", {
                        "expression": 'console.log("jury-native-execution-loop-start"); while (true) {}',
                        "timeout": 500,
                    }),
                    timeout=8,
                )
            )
        assert loop_started
        assert time.monotonic() - began < 8
        assert native_execution_terminated(raised.value)
        assert not native_execution_terminated(RuntimeError(str(raised.value)))
        assert not native_execution_terminated(PlaywrightError("Unrelated protocol error"))
    finally:
        context.close()
    assert context not in disposable_browser.contexts
