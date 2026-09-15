"""Unit tests for the Safari-backed browser automation surface."""

# Code version: v2.5.0-codex.1

from __future__ import annotations

import base64
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from unittest.mock import patch

import pytest

from app.core.safari_automation import (
    SafariContext,
    SafariNativeActivationError,
    SafariPage,
    run_applescript,
    safari_navigation_matches,
)


def test_safari_page_downloads_one_authenticated_range(tmp_path: Path) -> None:
    context = SafariContext("https://grok.com/files")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    destination = tmp_path / "asset.part"
    content = b"\xff\xd8\xff\xe0"
    metadata = {
        "state": "ready",
        "status": 206,
        "contentType": "image/jpeg",
        "contentRange": "bytes 0-3/4",
        "contentLength": "4",
        "bytes": len(content),
        "error": "",
    }

    with patch.object(page, "keep_rendering_in_background"), patch.object(
        page,
        "evaluate",
        side_effect=[
            True,
            metadata,
            base64.b64encode(content).decode(),
            True,
        ],
    ):
        content_type, resumed = page.download_to_path(
            "https://assets.grok.com/example/image.jpg",
            destination,
            lambda: False,
        )

    assert content_type == "image/jpeg"
    assert resumed is False
    assert destination.read_bytes() == content


def test_safari_page_restarts_when_a_stale_partial_gets_http_416(tmp_path: Path) -> None:
    context = SafariContext("https://grok.com/files")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    destination = tmp_path / "asset.part"
    destination.write_bytes(b"stale-partial")
    content = b"full-payload"
    rejected = {
        "state": "ready",
        "status": 416,
        "contentType": "",
        "contentRange": "bytes */12",
        "contentLength": "0",
        "bytes": 0,
        "error": "",
    }
    accepted = {
        "state": "ready",
        "status": 206,
        "contentType": "application/octet-stream",
        "contentRange": "bytes 0-11/12",
        "contentLength": "12",
        "bytes": len(content),
        "error": "",
    }

    with patch.object(page, "keep_rendering_in_background"), patch.object(
        page,
        "evaluate",
        side_effect=[True, rejected, True, True, accepted, base64.b64encode(content).decode(), True],
    ):
        content_type, resumed = page.download_to_path(
            "https://assets.grok.com/example/file.bin",
            destination,
            lambda: False,
        )

    assert content_type == "application/octet-stream"
    assert resumed is True
    assert destination.read_bytes() == content


def test_safari_page_evaluate_invokes_page_function_and_decodes_value() -> None:
    context = SafariContext("https://grok.com/files")
    page = SafariPage(context, window_id=123)
    encoded_result = '{"ok":true,"value":{"status":200}}'

    with patch.object(page, "_run_in_window", return_value=encoded_result) as run:
        result = page.evaluate("(request) => ({ status: request.status })", {"status": 200})

    assert result == {"status": 200}
    assert "do JavaScript" in run.call_args.args[0]


def test_safari_request_client_fetches_chunked_authenticated_text() -> None:
    context = SafariContext("https://chatgpt.com/")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    body_text = '{"items":[]}'

    with patch.object(
        page,
        "evaluate",
        side_effect=[
            True,
            {
                "state": "ready",
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "bodyLength": len(body_text),
                "error": "",
            },
            {"text": body_text, "end": len(body_text)},
            True,
        ],
    ) as evaluate:
        response = context.request.get(
            "https://chatgpt.com/backend-api/test",
            timeout=60_000,
            headers={
                "Authorization": "Bearer test-token",
                "Referer": "https://chatgpt.com/",
            },
        )

    assert response.ok
    assert response.status == 200
    assert response.text() == body_text
    assert response.headers == {"content-type": "application/json"}
    assert response.request.headers["Authorization"] == "Bearer test-token"
    request_argument = evaluate.call_args_list[0].args[1]
    assert request_argument["headers"] == {"Authorization": "Bearer test-token"}
    assert request_argument["referrer"] == "https://chatgpt.com/"


def test_safari_request_client_returns_small_responses_inline() -> None:
    context = SafariContext("https://chatgpt.com/")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    body_text = '{"accessToken":"redacted"}'

    with patch.object(
        page,
        "evaluate",
        side_effect=[
            True,
            {
                "state": "ready",
                "status": 200,
                "headers": {},
                "bodyLength": len(body_text),
                "bodyText": body_text,
                "cleanedInline": True,
                "error": "",
            },
        ],
    ) as evaluate:
        response = context.request.get(
            "https://chatgpt.com/api/auth/session",
            timeout=60_000,
        )

    assert response.text() == body_text
    assert evaluate.call_count == 2


def test_safari_request_client_can_bind_a_request_to_one_owned_page() -> None:
    context = SafariContext("https://chatgpt.com/")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    expected_response = object()

    with patch.object(context.request, "_get_once", return_value=expected_response) as get_once:
        response = context.request.get_from_page(
            page,
            "https://chatgpt.com/backend-api/conversation/demo",
            timeout=60_000,
            headers={"Accept": "application/json"},
        )

    assert response is expected_response
    get_once.assert_called_once_with(
        page,
        "https://chatgpt.com/backend-api/conversation/demo",
        60_000,
        {"Accept": "application/json"},
        serialize=False,
    )


def test_safari_request_client_posts_authenticated_json_from_one_owned_page() -> None:
    context = SafariContext("https://grok.com/")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    body_text = '{"responses":[]}'

    with patch.object(
        page,
        "evaluate",
        side_effect=[
            True,
            {
                "state": "ready",
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "bodyLength": len(body_text),
                "bodyText": body_text,
                "cleanedInline": True,
                "error": "",
            },
        ],
    ) as evaluate:
        response = context.request.request_from_page(
            page,
            "https://grok.com/rest/app-chat/conversations/demo/load-responses",
            timeout=30_000,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
            body='{"responseIds":["response-1"]}',
        )

    assert response.status == 200
    assert response.text() == body_text
    request_argument = evaluate.call_args_list[0].args[1]
    assert request_argument["method"] == "POST"
    assert request_argument["body"] == '{"responseIds":["response-1"]}'
    assert request_argument["headers"]["Content-Type"] == "application/json"
    request_script = evaluate.call_args_list[0].args[0]
    assert "targetUrl.origin !== location.origin" in request_script
    assert 'redirect: "error"' in request_script
    assert "fetch(targetUrl.href, options)" in request_script
    assert "if (response.redirected)" in request_script
    assert "if (response.url)" in request_script


def test_safari_request_client_rejects_cross_origin_inside_the_page() -> None:
    context = SafariContext("https://grok.com/")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)

    with patch.object(
        page,
        "evaluate",
        side_effect=[
            False,
            {
                "state": "failed",
                "status": 0,
                "headers": {},
                "bodyLength": 0,
                "error": "Safari request must remain same-origin.",
            },
            True,
        ],
    ) as evaluate, pytest.raises(RuntimeError, match="must remain same-origin"):
        context.request.get_from_page(
            page,
            "https://example.com/private",
            timeout=1_000,
        )

    request_script = evaluate.call_args_list[0].args[0]
    assert "targetUrl.origin !== location.origin" in request_script
    assert "fetch(targetUrl.href, options)" in request_script


def test_safari_request_client_recovers_from_suspended_window_fetch_failure() -> None:
    context = SafariContext("https://chatgpt.com/")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    expected_response = object()

    with (
        patch.object(
            context.request,
            "_get_once",
            side_effect=[RuntimeError("Safari request failed: Load failed"), expected_response],
        ),
        patch.object(page, "keep_rendering_in_background") as keep_rendering,
        patch("app.core.safari_automation.time.sleep"),
    ):
        response = context.request.get_from_page(
            page,
            "https://chatgpt.com/api/auth/session",
            timeout=60_000,
        )

    assert response is expected_response
    keep_rendering.assert_called_once_with()


def test_safari_page_exposes_shared_readiness_helpers() -> None:
    context = SafariContext("https://chatgpt.com/")
    page = SafariPage(context, window_id=123)

    with patch.object(
        page,
        "evaluate",
        side_effect=["ChatGPT", {"found": True, "text": "Ready"}],
    ):
        assert page.title() == "ChatGPT"
        assert page.locator("body").inner_text(timeout=1_000) == "Ready"

    assert page.context is context


def test_safari_locator_exposes_bounded_grok_model_control_operations() -> None:
    context = SafariContext("https://grok.com/")
    page = SafariPage(context, window_id=123)

    with patch.object(
        page,
        "evaluate",
        side_effect=[
            2,
            1,
            True,
            {"actionable": True, "currentUrl": "https://grok.com/"},
            "true",
            {"found": True, "visible": True},
            {"found": True, "text": "Build Beta"},
        ],
    ) as evaluate, patch.object(page, "_activate_marked_element") as activate:
        locator = page.locator("[data-cachelikes-grok-model-trigger]")
        assert locator.count() == 2
        assert locator.first.count() == 1
        assert locator.last.is_visible() is True
        locator.nth(1).click(
            timeout=1_000,
            expected_url="https://grok.com/",
        )
        assert locator.first.get_attribute("aria-expanded") == "true"
        locator.first.wait_for(state="visible", timeout=1_000)
        assert locator.first.inner_text(timeout=1_000) == "Build Beta"

    observed_indexes = [
        call.args[1]["index"]
        for call in evaluate.call_args_list
    ]
    assert observed_indexes == [None, 0, -1, 1, 0, 0, 0]
    click_script = evaluate.call_args_list[3].args[0]
    click_argument = evaluate.call_args_list[3].args[1]
    assert "scrollIntoView" in click_script
    assert "document.elementFromPoint" in click_script
    assert "element.contains(hit)" in click_script
    assert "element.click()" not in click_script
    assert click_argument["expectedCurrentUrl"] == "https://grok.com/"
    activation_marker, activation_url = activate.call_args.args
    assert activation_marker.startswith("safari-native-")
    assert activation_url == "https://grok.com/"


def test_safari_native_activation_is_origin_bound_trusted_and_non_retried() -> None:
    context = SafariContext("https://grok.com/")
    page = SafariPage(context, window_id=123)

    with patch.object(page, "_run_in_window", return_value="trusted") as run:
        page._activate_marked_element("safari-native-abc", "https://grok.com/")

    script = run.call_args.args[0]
    assert "key code 36" in script
    assert "event.isTrusted" in script
    assert "expected.port" in script
    assert "current.port" in script
    assert "expected.href !== current.href" in script
    assert script.count("document.hasFocus()") == 4
    assert script.count("document.elementFromPoint") == 2
    assert "currentFrontmostProcessName is not \"Safari\"" in script
    assert "count of sheets of front window" in script
    assert 'count of windows whose subrole is "AXDialog"' in script
    assert "set targetTab to current tab of targetWindow" in script
    assert "(id of front window) is not (id of targetWindow)" in script
    assert "(current tab of targetWindow) is not targetTab" in script
    assert "set shouldRestoreNativeFocus to (current tab of targetWindow) is targetTab" in script
    assert 'do JavaScript "document.hasFocus()" in targetTab' in script
    final_verification = script.rindex("set finalActivationState")
    final_window_check = script.rindex(
        "if (id of front window) is not (id of targetWindow)"
    )
    native_return = script.index("key code 36")
    assert final_verification < final_window_check < native_return
    assert script.index("count of sheets of front window") < native_return
    assert script.index('count of windows whose subrole is "AXDialog"') < native_return
    assert run.call_args.kwargs == {"retry_transient": False}


@pytest.mark.parametrize(
    ("message", "input_attempted"),
    (
        ("SAFARI_NATIVE_INPUT_NOT_ATTEMPTED: blocked", False),
        ("SAFARI_NATIVE_INPUT_UNCERTAIN: timed out", True),
        ("Apple event timed out", True),
    ),
)
def test_safari_native_activation_classifies_transport_uncertainty(
    message: str,
    input_attempted: bool,
) -> None:
    page = SafariPage(SafariContext("https://grok.com/"), window_id=123)

    with patch.object(page, "_run_in_window", side_effect=RuntimeError(message)):
        with pytest.raises(SafariNativeActivationError) as exc_info:
            page._activate_marked_element(
                "safari-native-abc",
                "https://grok.com/",
            )

    assert exc_info.value.input_attempted is input_attempted


def test_safari_locator_rejects_url_drift_before_any_native_input() -> None:
    page = SafariPage(SafariContext("https://grok.com/"), window_id=123)

    with patch.object(
        page,
        "evaluate",
        return_value={
            "actionable": False,
            "targetMismatch": True,
            "currentUrl": "https://grok.com/c/unexpected",
        },
    ) as evaluate, patch.object(page, "_activate_marked_element") as activate:
        with pytest.raises(RuntimeError, match="target changed"):
            page.locator("button").click(
                timeout=1_000,
                expected_url="https://grok.com/",
            )

    assert evaluate.call_args.args[1]["expectedCurrentUrl"] == "https://grok.com/"
    activate.assert_not_called()


@pytest.mark.parametrize(
    "url",
    (
        "http://grok.com/",
        "https://grok.com:444/",
        "https://user:pass@grok.com/",
        "https://grok.com:invalid/",
        "https://example.com/",
    ),
)
def test_safari_native_activation_rejects_untrusted_origins_before_input(url: str) -> None:
    page = SafariPage(SafariContext("https://grok.com/"), window_id=123)

    with patch.object(page, "_run_in_window") as run, pytest.raises(
        RuntimeError,
        match="refused native input",
    ):
        page._activate_marked_element("safari-native-abc", url)

    run.assert_not_called()


def test_safari_navigation_rejects_cross_scheme_provider_drift() -> None:
    assert not safari_navigation_matches("https://grok.com/", "http://grok.com/")
    assert safari_navigation_matches("https://grok.com/", "https://grok.com/")


def test_safari_navigation_scopes_openai_auth_redirects_to_chatgpt() -> None:
    assert safari_navigation_matches(
        "https://chatgpt.com/",
        "https://auth.openai.com/authorize",
    )
    assert not safari_navigation_matches(
        "https://grok.com/",
        "https://auth.openai.com/authorize",
    )


def test_safari_page_can_remain_render_active_in_background_without_stealing_focus() -> None:
    context = SafariContext("https://gemini.google.com/app")
    page = SafariPage(context, window_id=123)

    with patch.object(page, "_run_in_window", return_value="") as run:
        page.keep_rendering_in_background()

    script = run.call_args.args[0]
    assert "set previousWindowId to id of front window" in script
    assert "set miniaturized of targetWindow to false" in script
    assert "set visible of targetWindow to true" in script
    assert "set bounds of targetWindow" not in script
    assert "previousFrontmostProcessName" in script
    assert "frontmost of process previousFrontmostProcessName" in script
    assert "set index of (first window whose id is previousWindowId) to 1" in script


def test_safari_page_marks_rendering_active_after_background_restore() -> None:
    context = SafariContext("https://chatgpt.com/")
    page = SafariPage(context, window_id=123)

    with patch.object(page, "_run_in_window", return_value=""):
        page.keep_rendering_in_background()

    assert page._rendering_active is True


def test_safari_page_restarts_a_resume_when_server_returns_the_wrong_range(tmp_path: Path) -> None:
    context = SafariContext("https://chatgpt.com/")
    page = SafariPage(context, window_id=123)
    destination = tmp_path / "asset.part"
    destination.write_bytes(b"stale")
    payload = b"fresh"
    wrong_range = {
        "state": "ready",
        "status": 206,
        "contentType": "image/png",
        "contentRange": "bytes 0-4/5",
        "bytes": len(payload),
    }
    correct_range = {
        "state": "ready",
        "status": 200,
        "contentType": "image/png",
        "contentRange": "",
        "bytes": len(payload),
    }

    with patch.object(page, "keep_rendering_in_background"), patch.object(
        page,
        "evaluate",
        side_effect=[True, wrong_range, True, correct_range, "ZnJlc2g=", True],
    ):
        content_type, resumed = page.download_to_path(
            "https://chatgpt.com/image.png",
            destination,
            lambda: False,
        )

    assert content_type == "image/png"
    assert resumed is True
    assert destination.read_bytes() == payload


def test_safari_page_compatibility_background_method_keeps_window_available() -> None:
    context = SafariContext("https://gemini.google.com/app")
    page = SafariPage(context, window_id=123)

    with patch.object(page, "_run_in_window", return_value="") as run:
        page.keep_background()

    script = run.call_args.args[0]
    assert "set visible of targetWindow to true" in script
    assert "set miniaturized of targetWindow to false" in script
    assert "set bounds of targetWindow" not in script
    assert "set index of targetWindow" not in script


def test_safari_page_does_not_spawn_a_replacement_when_closed_externally() -> None:
    context = SafariContext("https://grok.com/files")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)

    with patch(
        "app.core.safari_automation.run_applescript",
        side_effect=RuntimeError(
            "Safari got an error: Can't get window 1 whose id = 123. Invalid index. (-1719)"
        ),
    ) as run, pytest.raises(RuntimeError, match="cache window was closed"):
        _ = page.url

    assert page.window_id == 123
    assert run.call_count == 1


def test_safari_page_reports_a_window_that_cannot_be_closed() -> None:
    context = SafariContext("https://gemini.google.com/app")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    close_error = RuntimeError("Safari window 123 remained open.")

    with patch.object(page, "_close_owned_window", return_value=close_error), pytest.raises(
        RuntimeError,
        match="remained open",
    ):
        page.close()

    assert page not in context.pages
    assert page._closed is True


def test_run_applescript_retries_transient_safari_errors() -> None:
    failed = type("Process", (), {"returncode": 1, "stderr": "execution error (-1712)", "stdout": ""})()
    succeeded = type("Process", (), {"returncode": 0, "stderr": "", "stdout": "ok\n"})()

    with patch("app.core.safari_automation.subprocess.run", side_effect=[failed, succeeded]), patch(
        "app.core.safari_automation.time.sleep"
    ) as sleep:
        assert run_applescript("return true") == "ok"

    sleep.assert_called_once()


def test_run_applescript_bounds_a_hung_safari_event() -> None:
    with patch(
        "app.core.safari_automation.subprocess.run",
        side_effect=subprocess.TimeoutExpired("osascript", 20),
    ), patch("app.core.safari_automation.time.sleep"), pytest.raises(
        RuntimeError,
        match="timed out",
    ):
        run_applescript("return true")


def test_run_applescript_never_retries_native_mutating_input() -> None:
    failed = type(
        "Process",
        (),
        {"returncode": 1, "stderr": "execution error (-1712)", "stdout": ""},
    )()

    with patch("app.core.safari_automation.subprocess.run", return_value=failed) as run, patch(
        "app.core.safari_automation.time.sleep"
    ), pytest.raises(RuntimeError, match="-1712"):
        run_applescript("key code 36", retry_transient=False)

    run.assert_called_once()


def test_safari_context_housekeeping_closes_all_owned_windows() -> None:
    context = SafariContext("https://grok.com/files")
    first_page = SafariPage(context, window_id=123)
    second_page = SafariPage(context, window_id=456)
    context.pages.extend([first_page, second_page])

    with patch.object(
        first_page,
        "_run_in_window",
        return_value="closed",
    ), patch.object(
        second_page,
        "_run_in_window",
        return_value="closed",
    ):
        assert context.housekeep() == 2

    assert context.pages == []
    assert first_page._closed is True
    assert second_page._closed is True


def test_safari_context_housekeeping_continues_after_one_close_failure() -> None:
    context = SafariContext("https://grok.com/files")
    first_page = SafariPage(context, window_id=123)
    second_page = SafariPage(context, window_id=456)
    context.pages.extend([first_page, second_page])

    with patch.object(first_page, "_close_owned_window", return_value=RuntimeError("still-open")), patch.object(
        second_page,
        "_run_in_window",
        return_value="closed",
    ):
        with pytest.raises(RuntimeError, match="housekeeping failed"):
            context.housekeep()

    assert context.pages == []
    assert first_page._closed is True
    assert second_page._closed is True


def test_safari_context_creates_a_standard_visible_background_window() -> None:
    context = SafariContext("https://grok.com/files")

    with patch("app.core.safari_automation.run_applescript", return_value="123") as run, patch.object(
        SafariPage,
        "goto",
    ) as goto:
        page = context._create_page("https://grok.com/files")

    script = run.call_args.args[0]
    assert page.window_id == 123
    goto.assert_called_once_with(
        "https://grok.com/files",
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    assert "set previousWindowId to 0" in script
    assert "previousFrontmostProcessName" in script
    assert "set previousWindowWasVisible to visible of front window" in script
    assert "set previousWindowWasMiniaturized to miniaturized of front window" in script
    assert script.index("set previousFrontmostProcessName") < script.index("launch")
    assert "existingWindowIds" in script
    assert "set targetWindow to candidateWindow" in script
    assert "emptyWindowIds" in script
    assert "set visible of targetWindow to true" in script
    assert "set miniaturized of targetWindow to false" in script
    assert "set bounds of targetWindow" not in script
    assert 'Safari did not create an owned window.' in script
    assert (
        "if previousWindowId is not 0 and previousWindowWasVisible and not "
        "previousWindowWasMiniaturized then"
    ) in script
    assert script.index("set URL of current tab of targetWindow") < script.index(
        "set miniaturized of targetWindow to false"
    )


def test_safari_page_content_clips_source_after_reading_the_owned_tab() -> None:
    context = SafariContext("https://grok.com/files")
    page = SafariPage(context, window_id=123)

    with patch.object(page, "_run_in_window", return_value="0123456789") as run:
        assert page.content(limit=4) == "0123"

    assert run.call_args.args[0] == "return source of current tab of targetWindow"


def test_safari_page_navigation_retries_a_start_page_and_verifies_the_target_url() -> None:
    context = SafariContext("https://chatgpt.com/project")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    states = [
        {"readyState": "complete", "href": "favorites://"},
        {"readyState": "complete", "href": "favorites://"},
        {"readyState": "complete", "href": "favorites://"},
        {
            "readyState": "interactive",
            "href": "https://chatgpt.com/project",
        },
    ]

    with patch.object(page, "_read_navigation_state", side_effect=states), patch.object(
        page,
        "_run_in_window",
        return_value="",
    ) as run, patch(
        "app.core.safari_automation.SAFARI_WRONG_PAGE_GRACE_SECONDS",
        0,
    ), patch.object(page, "_keep_in_background") as keep_in_background:
        page.goto("https://chatgpt.com/project", timeout=5_000)

    assert run.call_count == 2
    keep_in_background.assert_called_once_with()


def test_safari_page_reads_navigation_state_without_json_wrapping() -> None:
    context = SafariContext("https://chatgpt.com/project")
    page = SafariPage(context, window_id=123)

    with patch.object(
        page,
        "_run_in_window",
        return_value="https://chatgpt.com/project\ninteractive",
    ) as run:
        state = page._read_navigation_state()

    assert state == {
        "href": "https://chatgpt.com/project",
        "readyState": "interactive",
    }
    assert "URL of current tab" in run.call_args.args[0]
    assert "document.readyState" in run.call_args.args[0]


def test_safari_page_close_closes_the_owned_window() -> None:
    context = SafariContext("https://chatgpt.com/project")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)

    with patch.object(
        page,
        "_run_in_window",
        return_value="closed",
    ) as run:
        page.close()

    script = run.call_args.args[0]
    assert "click button 1 of front window" in script
    assert "if not (exists (first window whose id is 123))" in script
    assert 'return "closed"' in script
    assert "set URL of current tab of targetWindow" not in script
    assert page._closed is True
    assert context.pages == []


def test_safari_context_serializes_concurrent_window_creation() -> None:
    contexts = [
        SafariContext("https://chatgpt.com/"),
        SafariContext("https://chatgpt.com/"),
    ]
    counter_lock = Lock()
    active_calls = 0
    maximum_active_calls = 0
    next_window_id = 100

    def create_window(_source: str) -> str:
        nonlocal active_calls, maximum_active_calls, next_window_id
        with counter_lock:
            active_calls += 1
            maximum_active_calls = max(maximum_active_calls, active_calls)
            next_window_id += 1
            window_id = next_window_id
        time.sleep(0.05)
        with counter_lock:
            active_calls -= 1
        return str(window_id)

    with patch("app.core.safari_automation.run_applescript", side_effect=create_window), patch.object(
        SafariPage,
        "goto",
    ):
        with ThreadPoolExecutor(max_workers=2) as executor:
            pages = list(
                executor.map(
                    lambda context: context._create_page("https://chatgpt.com/"),
                    contexts,
                )
            )

    assert maximum_active_calls == 1
    assert {page.window_id for page in pages} == {101, 102}


def test_safari_context_holds_one_cross_process_lease_for_its_lifetime(tmp_path: Path) -> None:
    lock_path = tmp_path / "safari-context.lock"
    first_context = SafariContext("https://gemini.google.com/app")
    second_context = SafariContext("https://grok.com/files")
    second_entered = False

    def enter_second_context() -> None:
        nonlocal second_entered
        second_context._acquire_context_lock()
        second_entered = True

    with patch("app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH", lock_path):
        first_context._acquire_context_lock()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(enter_second_context)
            time.sleep(0.05)
            assert second_entered is False

            first_context._release_context_lock()
            pending.result(timeout=1)

        assert second_entered is True
        second_context._release_context_lock()


def test_safari_context_can_fail_fast_when_another_task_owns_the_lease(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    first_context = SafariContext("https://grok.com/")
    probe_context = SafariContext("https://grok.com/", lock_blocking=False)

    with patch("app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH", lock_path):
        first_context._acquire_context_lock()
        try:
            with pytest.raises(RuntimeError, match="Safari is busy"):
                probe_context._acquire_context_lock()
        finally:
            first_context._release_context_lock()

    assert probe_context._context_lock_handle is None
