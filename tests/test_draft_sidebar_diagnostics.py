"""Real-renderer regressions for bounded draft Sidebar failure diagnostics.

Code version: v1.0.0-codex.0
"""

import json
import time

import pytest
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from tests import test_sidebar_e2e as fixtures
from tests.test_agent_sessions_e2e import _observe_draft_sidebar_failure


disposable_browser = fixtures.disposable_browser


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("failure_kind", ("assertion", "navigation_timeout"))
def test_renderer_loop_preserves_original_failure_and_releases_context(
    disposable_browser, capsys, monkeypatch, failure_kind
):
    import asyncio

    context = disposable_browser.new_context()
    page = context.new_page()
    original_evaluate = Page.evaluate
    loop_started = []
    attempts = []
    cancelled = []
    native_evaluate = page._impl_obj.evaluate

    async def observe_evaluation(expression):
        attempts.append(expression)
        try:
            return await native_evaluate(expression)
        except asyncio.CancelledError:
            cancelled.append(expression)
            raise

    monkeypatch.setattr(page._impl_obj, "evaluate", observe_evaluation)
    page.on(
        "console",
        lambda message: loop_started.append(True)
        if message.text == "draft-sidebar-diagnostic-loop-start"
        else None,
    )
    context.route(
        "http://127.0.0.1:49112/probe",
        lambda route: route.fulfill(
            content_type="text/html",
            body='<script>console.log("draft-sidebar-diagnostic-loop-start"); while (true) {}</script>',
        ),
    )
    original_error = None
    began = None
    diagnostic_seconds = None
    error_type = AssertionError if failure_kind == "assertion" else PlaywrightTimeoutError
    try:
        with pytest.raises(error_type) as raised:
            try:
                with _observe_draft_sidebar_failure(page):
                    try:
                        # Only this new synthetic fault uses a short navigation budget.
                        page.goto("http://127.0.0.1:49112/probe", timeout=1_000)
                    except PlaywrightTimeoutError as error:
                        assert loop_started, "The renderer must enter the actual infinite loop."
                        original_error = (
                            AssertionError("Injected draft interaction failure")
                            if failure_kind == "assertion" else error
                        )
                        began = time.monotonic()
                        raise original_error
            finally:
                if began is not None:
                    diagnostic_seconds = time.monotonic() - began
                context.close()
        assert raised.value is original_error
        assert Page.evaluate is original_evaluate
        assert len(attempts) == len(cancelled) == 2
        assert diagnostic_seconds < 3
        assert context not in disposable_browser.contexts
        diagnostic = capsys.readouterr().out.split("Draft sidebar failure diagnostic: ", 1)[1]
        assert json.loads(diagnostic)["capture_error_type"] == "TimeoutError"
    finally:
        context.close()
