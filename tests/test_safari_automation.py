"""Unit tests for the Safari-backed browser automation surface."""

# Code version: v2.15.0-codex.0

from __future__ import annotations

import base64
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from unittest.mock import patch

import pytest

from app.core.safari_automation import (
    SAFARI_CONTEXT_CREATION_SETTLE_SECONDS,
    SAFARI_CONTEXT_LEASE_VERSION,
    SafariAuthenticationRequiredError,
    SafariContext,
    SafariNativeActivationError,
    SafariPage,
    retry_pending_safari_context_cleanup,
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
        "encoded": base64.b64encode(content).decode(),
        "nextOffset": len(content),
    }

    with patch.object(
        page,
        "evaluate",
        side_effect=[True, metadata],
    ):
        content_type, resumed = page.download_to_path(
            "https://assets.grok.com/example/image.jpg",
            destination,
            lambda: False,
        )

    assert content_type == "image/jpeg"
    assert resumed is False
    assert destination.read_bytes() == content


def test_safari_page_rejects_oversized_media_before_writing(tmp_path: Path) -> None:
    context = SafariContext("https://claude.ai/chats")
    page = SafariPage(context, window_id=123)
    destination = tmp_path / "asset.part"
    metadata = {
        "state": "ready",
        "status": 206,
        "contentType": "image/png",
        "contentRange": "bytes 0-3/9",
        "bytes": 4,
        "encoded": base64.b64encode(b"abcd").decode(),
        "nextOffset": 4,
    }

    with patch.object(page, "evaluate", side_effect=[True, metadata]) as evaluate:
        with pytest.raises(RuntimeError, match="8-byte cache limit"):
            page.download_to_path(
                "https://claude.ai/api/asset/image",
                destination,
                lambda: False,
                max_bytes=8,
            )

    assert not destination.exists()
    assert evaluate.call_args_list[0].args[1]["maxBytes"] == 8


def test_safari_page_rejects_oversized_partial_without_fetch(tmp_path: Path) -> None:
    page = SafariPage(SafariContext("https://claude.ai/chats"), window_id=123)
    destination = tmp_path / "asset.part"
    destination.write_bytes(b"too-large")

    with patch.object(page, "evaluate") as evaluate:
        with pytest.raises(RuntimeError, match="4-byte cache limit"):
            page.download_to_path(
                "https://claude.ai/api/asset/image",
                destination,
                lambda: False,
                max_bytes=4,
            )

    assert destination.read_bytes() == b"too-large"
    evaluate.assert_not_called()


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
        "encoded": base64.b64encode(content).decode(),
        "nextOffset": len(content),
    }

    with patch.object(
        page,
        "evaluate",
        side_effect=[True, rejected, True, accepted],
    ):
        content_type, resumed = page.download_to_path(
            "https://assets.grok.com/example/file.bin",
            destination,
            lambda: False,
        )

    assert content_type == "application/octet-stream"
    assert resumed is True
    assert destination.read_bytes() == content


def test_safari_page_finishes_when_cross_origin_hides_content_range(tmp_path: Path) -> None:
    context = SafariContext("https://grok.com/files")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    destination = tmp_path / "asset.part"
    content = b"short-tail"
    hidden_range = {
        "state": "ready",
        "status": 206,
        "contentType": "video/mp4",
        "contentRange": "",
        "contentLength": str(len(content)),
        "bytes": len(content),
        "error": "",
        "encoded": base64.b64encode(content).decode(),
        "nextOffset": len(content),
    }

    with patch.object(
        page,
        "evaluate",
        side_effect=[True, hidden_range],
    ) as evaluate:
        content_type, resumed = page.download_to_path(
            "https://assets.grok.com/example/generated_video.mp4",
            destination,
            lambda: False,
        )

    assert content_type == "video/mp4"
    assert resumed is False
    assert destination.read_bytes() == content
    assert evaluate.call_count == 2


def test_safari_page_treats_http_416_after_full_chunks_as_eof(tmp_path: Path) -> None:
    context = SafariContext("https://grok.com/files")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    destination = tmp_path / "asset.part"
    content = b"abcd"
    full_chunk = {
        "state": "ready",
        "status": 206,
        "contentType": "video/mp4",
        "contentRange": "",
        "bytes": len(content),
        "error": "",
        "encoded": base64.b64encode(content).decode(),
        "nextOffset": len(content),
    }
    past_eof = {"state": "ready", "status": 416, "contentRange": "", "bytes": 0, "error": ""}

    with patch("app.core.safari_automation.SAFARI_DOWNLOAD_RANGE_BYTES", len(content)), patch.object(
        page,
        "evaluate",
        side_effect=[True, full_chunk, True, past_eof],
    ):
        content_type, resumed = page.download_to_path(
            "https://assets.grok.com/example/generated_video.mp4",
            destination,
            lambda: False,
        )

    assert content_type == "video/mp4"
    assert resumed is False
    assert destination.read_bytes() == content


def test_safari_page_uses_expected_size_when_content_range_is_hidden(tmp_path: Path) -> None:
    context = SafariContext("https://grok.com/files")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    destination = tmp_path / "asset.part"
    content = b"abcd"
    full_chunk = {
        "state": "ready",
        "status": 206,
        "contentType": "image/jpeg",
        "contentRange": "",
        "bytes": len(content),
        "error": "",
        "encoded": base64.b64encode(content).decode(),
        "nextOffset": len(content),
    }

    with patch("app.core.safari_automation.SAFARI_DOWNLOAD_RANGE_BYTES", len(content)), patch.object(
        page,
        "evaluate",
        side_effect=[True, full_chunk],
    ):
        page.download_to_path(
            "https://assets.grok.com/example/content",
            destination,
            lambda: False,
            expected_bytes=len(content),
        )

    assert destination.read_bytes() == content


def test_safari_page_download_retries_never_activate_safari(tmp_path: Path) -> None:
    context = SafariContext("https://grok.com/files")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    page._background_only_depth = 1

    with patch.object(page, "keep_rendering_in_background") as keep_background, patch.object(
        page, "wake_for_javascript"
    ) as wake, patch.object(
        page, "_run_in_window", side_effect=RuntimeError("Safari automation timed out")
    ), patch("app.core.safari_automation.time.sleep"):
        with pytest.raises(RuntimeError):
            page.evaluate("() => true")

    wake.assert_not_called()
    keep_background.assert_not_called()


def test_safari_page_download_emits_only_non_mutating_background_applescript(tmp_path: Path) -> None:
    context = SafariContext("https://grok.com/files")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    destination = tmp_path / "asset.part"
    content = b"background-only"
    scripts: list[str] = []

    def run_script(source: str, **_kwargs: object) -> str:
        scripts.append(source)
        if "firstSliceEnd" in source:
            return json.dumps(
                {
                    "ok": True,
                    "value": {
                        "state": "ready",
                        "status": 200,
                        "contentType": "application/octet-stream",
                        "contentRange": "",
                        "contentLength": str(len(content)),
                        "bytes": len(content),
                        "error": "",
                        "encoded": base64.b64encode(content).decode(),
                        "nextOffset": len(content),
                    },
                }
            )
        return json.dumps({"ok": True, "value": True})

    with patch("app.core.safari_automation.run_applescript", side_effect=run_script):
        page.download_to_path(
            "https://assets.grok.com/example/file.bin",
            destination,
            lambda: False,
        )

    assert destination.read_bytes() == content
    assert len(scripts) == 2
    for source in scripts:
        lowered = source.casefold()
        assert "\nactivate\n" not in lowered
        assert "set frontmost of process" not in lowered
        assert "set index of" not in lowered
        assert "set visible of" not in lowered
        assert "set miniaturized of" not in lowered
        assert "set current tab of" not in lowered
    assert page._background_only_depth == 0


def test_safari_page_download_reports_http_authentication_failure_once(tmp_path: Path) -> None:
    context = SafariContext("https://grok.com/files")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    unauthorized = {
        "state": "ready",
        "status": 401,
        "contentType": "",
        "contentRange": "",
        "contentLength": "0",
        "bytes": 0,
        "error": "",
        "encoded": "",
        "nextOffset": 0,
    }

    with patch.object(page, "evaluate", side_effect=[True, unauthorized]) as evaluate:
        with pytest.raises(SafariAuthenticationRequiredError) as error:
            page.download_to_path(
                "https://assets.grok.com/example/file.bin",
                tmp_path / "asset.part",
                lambda: False,
            )

    assert error.value.status == 401
    assert evaluate.call_count == 2


def test_safari_page_background_transfer_rejects_window_mutation_before_execution() -> None:
    page = SafariPage(SafariContext("https://grok.com/files"), window_id=123)

    with patch("app.core.safari_automation.run_applescript") as run:
        with page._background_only_transfer(), pytest.raises(
            RuntimeError,
            match="cannot change foreground or window state",
        ):
            page._run_in_window("set visible of targetWindow to true")

    run.assert_not_called()


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

    with patch.object(
        page,
        "_run_in_window",
        side_effect=["Codex\n456\ntrue\nfalse", "trusted", ""],
    ) as run:
        page._activate_marked_element("safari-native-abc", "https://grok.com/")

    script = run.call_args_list[1].args[0]
    restore_script = run.call_args_list[2].args[0]
    assert "key code 36" in script
    assert "event.isTrusted" in script
    assert "expected.port" in script
    assert "current.port" in script
    assert "expected.href !== current.href" in script
    assert script.count("document.hasFocus()") == 3
    assert script.count("document.elementFromPoint") == 2
    assert "currentFrontmostProcessName is not \"Safari\"" in script
    assert "count of sheets of front window" in script
    assert "repeat with safariWindow in windows" in script
    assert '(subrole of safariWindow as text) is "AXDialog"' in script
    assert 'windows whose subrole is "AXDialog"' not in script
    assert "set current tab of targetWindow to targetTab" in script
    assert "set targetTab to current tab of targetWindow" not in script
    assert "nativeFocusReady" in script
    assert "(id of front window) is not (id of targetWindow)" in script
    assert "(current tab of targetWindow) is not targetTab" in script
    assert "set targetWindowStillFront" in restore_script
    assert '(id of front window) is (id of targetWindow)' in restore_script
    assert "(current tab of targetWindow) is targetTab" not in restore_script
    assert 'do JavaScript "document.hasFocus()" in targetTab' not in restore_script
    final_verification = script.rindex("set finalActivationState")
    final_window_check = script.rindex(
        "if (id of front window) is not (id of targetWindow)"
    )
    native_return = script.index("key code 36")
    assert final_verification < final_window_check < native_return
    assert script.index("nativeFocusReady") < native_return
    assert script.index("count of sheets of front window") < native_return
    assert script.index("repeat with safariWindow in windows") < native_return
    assert run.call_args_list[1].kwargs == {
        "retry_transient": False,
        "reveal_tab": True,
    }


def test_safari_locator_press_uses_one_bounded_trusted_navigation_key() -> None:
    context = SafariContext("https://chatgpt.com/")
    page = SafariPage(context, window_id=123)

    with patch.object(
        page,
        "evaluate",
        return_value={"actionable": True, "currentUrl": "https://chatgpt.com/"},
    ), patch.object(page, "_activate_marked_element") as activate:
        page.locator('[role="slider"]').press("ArrowRight", timeout=1_000)

    marker, target_url = activate.call_args.args
    assert marker.startswith("safari-native-")
    assert target_url == "https://chatgpt.com/"
    assert activate.call_args.kwargs == {"key": "ArrowRight"}


def test_safari_locator_escape_remains_bound_to_the_expected_url() -> None:
    context = SafariContext("https://grok.com/")
    page = SafariPage(context, window_id=123)

    with patch.object(
        page,
        "evaluate",
        return_value={"actionable": True, "currentUrl": "https://grok.com/"},
    ), patch.object(page, "_activate_marked_element") as activate:
        page.locator('[aria-haspopup="menu"]').press(
            "Escape",
            timeout=1_000,
            expected_url="https://grok.com/",
        )

    _marker, target_url = activate.call_args.args
    assert target_url == "https://grok.com/"
    assert activate.call_args.kwargs == {"key": "Escape"}


def test_safari_locator_evaluate_binds_one_selected_element() -> None:
    context = SafariContext("https://chatgpt.com/")
    page = SafariPage(context, window_id=123)

    with patch.object(page, "evaluate", return_value=["Instant", "Medium"]) as evaluate:
        result = page.locator('[role="slider"]').nth(2).evaluate(
            "(element, suffix) => [element.textContent, suffix]",
            "Medium",
        )

    assert result == ["Instant", "Medium"]
    source = evaluate.call_args.args[0]
    assert "const callback = ((element, suffix) => [element.textContent, suffix]);" in source
    assert "eval" not in source
    payload = evaluate.call_args.args[1]
    assert payload == {
        "selector": '[role="slider"]',
        "index": 2,
        "argument": "Medium",
    }


def test_safari_native_activation_binds_the_requested_navigation_key() -> None:
    page = SafariPage(SafariContext("https://chatgpt.com/"), window_id=123)

    with patch.object(
        page,
        "_run_in_window",
        side_effect=["Codex\n456\ntrue\nfalse", "trusted", ""],
    ) as run:
        page._activate_marked_element(
            "safari-native-slider",
            "https://chatgpt.com/",
            key="ArrowRight",
        )

    script = run.call_args_list[1].args[0]
    assert "expectedKey" in script
    assert "ArrowRight" in script
    assert "event.key === request.expectedKey" in script
    assert "key code 124" in script
    assert "key code 36" not in script


def test_safari_native_input_transaction_restores_after_tab_or_document_focus_changes() -> None:
    page = SafariPage(SafariContext("https://chatgpt.com/"), window_id=123)

    with patch.object(
        page,
        "_run_in_window",
        side_effect=["Codex\n456\ntrue\nfalse", "trusted", ""],
    ) as run:
        with page.native_input_transaction():
            page._activate_marked_element(
                "safari-native-slider",
                "https://chatgpt.com/",
                key="ArrowRight",
            )

    activation_script = run.call_args_list[1].args[0]
    restore_script = run.call_args_list[2].args[0]
    assert "set targetWindowStillFront" not in activation_script
    assert "set targetWindowStillFront" in restore_script
    assert 'currentFrontmostProcessName is "Safari"' in restore_script
    assert '(id of front window) is (id of targetWindow)' in restore_script
    assert "(current tab of targetWindow) is targetTab" not in restore_script
    assert "set current tab of targetWindow" not in restore_script
    assert 'do JavaScript "document.hasFocus()" in targetTab' not in restore_script
    assert "set bounds of targetWindow" not in restore_script
    assert "set miniaturized of targetWindow" not in restore_script
    assert "set visible of targetWindow" not in restore_script
    assert 'set previousFrontmostProcessName to "Codex"' in restore_script
    assert "set previousWindowId to 456" in restore_script
    assert "set previousWindowWasVisible to true" in restore_script
    assert "set previousWindowWasMiniaturized to false" in restore_script
    assert page._native_input_transaction_depth == 0


def test_safari_native_input_transaction_does_not_steal_focus_if_user_switched() -> None:
    page = SafariPage(SafariContext("https://chatgpt.com/"), window_id=123)

    with patch.object(
        page,
        "_run_in_window",
        side_effect=["Codex\n456\ntrue\nfalse", ""],
    ) as run:
        with page.native_input_transaction():
            pass

    restore_script = run.call_args_list[1].args[0]
    assert "set targetWindowStillFront" in restore_script
    assert "set targetWindowStillFront to false" in restore_script
    assert (
        'if currentFrontmostProcessName is "Safari" then\n'
        "    try\n"
        "        set targetWindowStillFront to (id of front window) is (id of targetWindow)"
    ) in restore_script
    assert restore_script.index(
        'currentFrontmostProcessName is "Safari"'
    ) < restore_script.index("frontmost of process previousFrontmostProcessName")
    assert (
        'if targetWindowStillFront and previousFrontmostProcessName is not "" '
        'and previousFrontmostProcessName is not "Safari" then'
    ) in restore_script
    assert "if currentFrontmostProcessName is not \"Safari\" then" not in restore_script


def test_safari_wake_for_javascript_uses_a_restorable_short_transaction() -> None:
    page = SafariPage(SafariContext("https://chatgpt.com/"), window_id=123)

    with patch.object(
        page,
        "_run_in_window",
        side_effect=["Codex\n456\ntrue\nfalse", "", ""],
    ) as run:
        page.wake_for_javascript()

    scripts = [call.args[0] for call in run.call_args_list]
    assert any("nativeFocusReady" in script for script in scripts)
    restore_script = scripts[-1]
    assert "set targetWindowStillFront" in restore_script
    assert 'set previousFrontmostProcessName to "Codex"' in restore_script
    assert page._native_input_transaction_depth == 0


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

    with patch.object(
        page,
        "_run_in_window",
        side_effect=[
            "Codex\n456\ntrue\nfalse",
            RuntimeError(message),
            "",
        ],
    ) as run:
        with pytest.raises(SafariNativeActivationError) as exc_info:
            page._activate_marked_element(
                "safari-native-abc",
                "https://grok.com/",
            )

    assert exc_info.value.input_attempted is input_attempted
    assert "set targetWindowStillFront" in run.call_args_list[2].args[0]


def test_safari_native_activation_preserves_uncertainty_when_focus_restore_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page = SafariPage(SafariContext("https://grok.com/"), window_id=123)

    with patch.object(
        page,
        "_run_in_window",
        side_effect=[
            "Codex\n456\ntrue\nfalse",
            RuntimeError("Apple event timed out"),
            RuntimeError("focus restore timed out"),
        ],
    ):
        with pytest.raises(SafariNativeActivationError) as exc_info:
            page._activate_marked_element(
                "safari-native-abc",
                "https://grok.com/",
            )

    assert exc_info.value.input_attempted is True
    assert "Apple event timed out" in str(exc_info.value)
    assert "could not restore native focus" in caplog.text


def test_safari_native_activation_keeps_success_when_focus_restore_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page = SafariPage(SafariContext("https://grok.com/"), window_id=123)

    with patch.object(
        page,
        "_run_in_window",
        side_effect=[
            "Codex\n456\ntrue\nfalse",
            "trusted",
            RuntimeError("focus restore timed out"),
        ],
    ):
        page._activate_marked_element(
            "safari-native-abc",
            "https://grok.com/",
        )

    assert "could not restore native focus after input completed" in caplog.text


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
        "encoded": base64.b64encode(payload).decode(),
        "nextOffset": len(payload),
    }
    correct_range = {
        "state": "ready",
        "status": 200,
        "contentType": "image/png",
        "contentRange": "",
        "bytes": len(payload),
        "encoded": base64.b64encode(payload).decode(),
        "nextOffset": len(payload),
    }

    with patch.object(
        page,
        "evaluate",
        side_effect=[True, wrong_range, True, correct_range],
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
    assert "targetWindowStillFront" in script
    assert "canRestorePreviousSafariWindow" not in script


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

    assert page in context.pages
    assert page._closed is False


def test_run_applescript_retries_transient_safari_errors() -> None:
    failed = type("Process", (), {"returncode": 1, "stderr": "execution error (-1712)", "stdout": ""})()
    succeeded = type("Process", (), {"returncode": 0, "stderr": "", "stdout": "ok\n"})()

    with patch("app.core.safari_automation.execute_applescript", side_effect=[failed, succeeded]), patch(
        "app.core.safari_automation.time.sleep"
    ) as sleep:
        assert run_applescript("return true") == "ok"

    sleep.assert_called_once()


def test_run_applescript_bounds_a_hung_safari_event() -> None:
    with patch(
        "app.core.safari_automation.execute_applescript",
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

    with patch("app.core.safari_automation.execute_applescript", return_value=failed) as run, patch(
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

    assert context.pages == [first_page]
    assert first_page._closed is False
    assert second_page._closed is True


def test_safari_context_retains_its_lease_until_failed_cleanup_retries(
    tmp_path: Path,
) -> None:
    context = SafariContext("https://chatgpt.com/")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    close_error = RuntimeError("Safari window 123 remained open.")

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        tmp_path / "safari-context.lock",
    ), patch.object(
        page,
        "_close_owned_window",
        side_effect=[close_error, None],
    ):
        context._acquire_context_lock()
        with pytest.raises(RuntimeError, match="housekeeping failed"):
            context.close()

        assert context._context_lock_handle is not None
        assert context.pages == [page]
        assert page._closed is False
        assert retry_pending_safari_context_cleanup() == (1, 0)

    assert context._context_lock_handle is None
    assert context.pages == []
    assert page._closed is True


def test_safari_reuses_an_owned_zero_tab_shell_after_page_cleanup(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    first = SafariContext("https://x.com/home", lock_blocking=False)
    first._creation_baseline_inventory = {123: 4}
    page = SafariPage(first, window_id=456)
    first.pages.append(page)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        return_value={123: 4, 456: 0},
    ), patch(
        "app.core.safari_automation._safari_process_identity",
        return_value="test-safari-session",
    ), patch.object(page, "_run_in_window", return_value="empty") as close_window:
        first._acquire_context_lock()
        first._mark_context_window_owned(456)
        first.close()

        state_path = lock_path.with_name("safari-context.lock.state")
        lease = json.loads(state_path.read_text(encoding="utf-8"))
        assert lease["state"] == "idle"
        assert lease["window_id"] == 456
        assert lease["baseline_windows"] == [{"window_id": 123, "tab_count": 4}]
        assert first.pages == []
        assert first._context_lock_handle is None
        assert (
            "count of tabs of (first window whose id is 456)"
            in close_window.call_args.args[0]
        )
        assert "(count of tabs of targetWindow) > 1" in close_window.call_args.args[0]
        first.close()

        second = SafariContext("https://x.com/home", lock_blocking=False)
        with patch.object(
            second, "_create_window", side_effect=AssertionError("new window")
        ), patch.object(second, "_create_tab", return_value=1) as create_tab, patch.object(
            SafariPage, "goto"
        ):
            second._acquire_context_lock()
            next_page = second._create_page("https://x.com/home")

        assert next_page.window_id == 456
        assert next_page.tab_index == 1
        create_tab.assert_called_once_with(
            456,
            "https://x.com/home",
            expect_empty_window=True,
        )
        assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "owned"
        with patch.object(next_page, "_run_in_window", return_value="empty"):
            second.close()

        with patch(
            "app.core.safari_automation._safari_window_inventory",
            return_value={123: 4},
        ):
            third = SafariContext("https://x.com/home", lock_blocking=False)
            third._acquire_context_lock()
            assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"
            third._release_context_lock()


def test_safari_idle_shell_waits_for_addressable_tab_before_navigation() -> None:
    context = SafariContext("https://x.com/home", lock_blocking=False)
    with patch(
        "app.core.safari_automation.run_applescript", return_value="1"
    ) as run:
        assert context._create_tab(456, "https://x.com/home", expect_empty_window=True) == 1

    script = run.call_args.args[0]
    assert script.index("make new tab") < script.index("repeat with prepareIndex")
    assert script.index("repeat with prepareIndex") < script.index("set URL of newTab")
    assert script.index("set newTab to tab 1 of targetWindow") < script.index("set URL of newTab")
    assert script.index("if not newTabReady then") < script.index("set current tab of targetWindow to newTab")
    assert "set readyTabIndex to index of tab 1 of targetWindow" in script
    assert "if readyTabIndex is 1 then" in script
    assert 'error "Safari idle task tab did not become addressable."' in script
    assert "if ownedTabCount > 1 then" in script
    assert script.index("set URL of newTab") < script.index("repeat with settleIndex")
    assert script.index("set current tab of targetWindow to newTab") < script.index(
        "repeat with settleIndex"
    )
    assert 'error "Safari idle task window gained an extra tab."' in script
    assert "close newTab" in script
    assert "close targetWindow" not in script


def test_safari_new_tab_in_populated_owned_window_does_not_rebind_first_tab() -> None:
    context = SafariContext("https://x.com/home", lock_blocking=False)
    with patch("app.core.safari_automation.run_applescript", return_value="2") as run:
        assert context._create_tab(456, "https://x.com/home") == 2

    script = run.call_args.args[0]
    assert "repeat with prepareIndex" not in script
    assert "set newTab to tab 1 of targetWindow" not in script
    assert "return index of newTab" in script
    assert "close targetWindow" not in script


def test_safari_idle_shell_reads_settled_current_tab_index() -> None:
    context = SafariContext("https://x.com/home", lock_blocking=False)
    with patch(
        "app.core.safari_automation.run_applescript", return_value="1"
    ) as run:
        assert context._create_tab(456, "https://x.com/home", expect_empty_window=True) == 1

    script = run.call_args.args[0]
    assert script.index("if (count of tabs of targetWindow) is not 1 then") < script.index(
        "set settledTabIndex to index of current tab of targetWindow"
    )
    assert "if settledTabIndex is not 1 then" in script
    assert "return settledTabIndex" in script
    assert "return index of newTab" not in script

    with patch("app.core.safari_automation.run_applescript", return_value="2"):
        with pytest.raises(RuntimeError, match="unexpected tab index"):
            context._create_tab(456, "https://x.com/home", expect_empty_window=True)


def test_safari_idle_shell_with_new_tab_is_not_closed_or_adopted(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "idle",
                "ownership_token": "a" * 32,
                "owner_pid": 123,
                "baseline_windows": [{"window_id": 123, "tab_count": 4}],
                "window_id": 456,
                "safari_process_identity": "test-safari-session",
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://x.com/home", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        return_value={123: 4, 456: 1},
    ), patch(
        "app.core.safari_automation._safari_process_identity",
        return_value="test-safari-session",
    ), patch(
        "app.core.safari_automation._close_safari_window_id",
        side_effect=AssertionError("The occupied shell must not be closed"),
    ), pytest.raises(RuntimeError, match="former task window now contains a tab"):
        context._acquire_context_lock()

    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "idle"
    assert context._context_lock_handle is None

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        return_value={123: 4, 456: 1},
    ), patch(
        "app.core.safari_automation._safari_process_identity",
        return_value="new-safari-session",
    ), patch(
        "app.core.safari_automation._close_safari_window_id",
        side_effect=AssertionError("A window from another Safari process must not be closed"),
    ):
        next_context = SafariContext("https://x.com/home", lock_blocking=False)
        next_context._acquire_context_lock()
        assert next_context._adopted_window_id is None
        assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"
        next_context._release_context_lock()


def test_safari_adopts_a_legacy_owned_zero_tab_shell(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "owned",
                "ownership_token": "a" * 32,
                "owner_pid": 123,
                "baseline_windows": [{"window_id": 123, "tab_count": 4}],
                "window_id": 456,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://x.com/home", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_pid_is_alive",
        return_value=False,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        return_value={123: 4, 456: 0},
    ), patch(
        "app.core.safari_automation._safari_process_identity",
        return_value="test-safari-session",
    ), patch(
        "app.core.safari_automation._close_safari_window_id",
        side_effect=AssertionError("The empty shell must be reused"),
    ), patch.object(context, "_create_tab", return_value=1), patch.object(
        SafariPage, "goto"
    ):
        context._acquire_context_lock()
        assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "idle"
        page = context._create_page("https://x.com/home")

        assert page.window_id == 456
        assert json.loads(state_path.read_text(encoding="utf-8"))["owner_pid"] != 123
        with patch.object(page, "_run_in_window", return_value="empty"):
            context.close()


def test_safari_legacy_empty_shell_does_not_close_a_new_user_tab(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "owned",
                "ownership_token": "a" * 32,
                "owner_pid": 123,
                "baseline_windows": [{"window_id": 123, "tab_count": 4}],
                "window_id": 456,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://x.com/home", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_pid_is_alive",
        return_value=False,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        side_effect=({123: 4, 456: 0}, {123: 4, 456: 1}),
    ), patch(
        "app.core.safari_automation._safari_process_identity",
        return_value="test-safari-session",
    ), patch(
        "app.core.safari_automation._close_safari_window_id",
        side_effect=AssertionError("The new tab must not be closed"),
    ), patch.object(
        context, "_create_tab", side_effect=AssertionError("No task tab may be added")
    ), pytest.raises(RuntimeError, match="former task window now contains a tab"):
        context._acquire_context_lock()
        assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "idle"
        context._create_page("https://x.com/home")

    context._release_context_lock()
    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "idle"


def test_safari_empty_shell_tab_creation_checks_for_user_tabs() -> None:
    context = SafariContext("https://x.com/home")
    with patch(
        "app.core.safari_automation.run_applescript",
        return_value="1",
    ) as run:
        assert context._create_tab(
            456,
            "https://x.com/home",
            expect_empty_window=True,
        ) == 1

    script = run.call_args.args[0]
    assert "if (count of tabs of targetWindow) is not 0 then" in script
    assert "if (count of tabs of targetWindow) is not 1 then" in script
    assert script.index("is not 0 then") < script.index("make new tab")
    assert script.index("make new tab") < script.index("is not 1 then")


def test_safari_idle_transition_can_finish_page_bookkeeping_on_retry(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    context = SafariContext("https://x.com/home", lock_blocking=False)
    page = SafariPage(context, window_id=456)
    context.pages.append(page)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        return_value={456: 0},
    ), patch(
        "app.core.safari_automation._safari_process_identity",
        return_value="test-safari-session",
    ), patch.object(
        page, "_run_in_window", side_effect=AssertionError("The empty shell must not be closed")
    ):
        context._acquire_context_lock()
        context._mark_context_window_owned(456)
        context._mark_context_window_idle(456)
        context._retained_empty_window_id = None
        context.close()

    assert page._closed is True
    assert context.pages == []
    assert context._context_lock_handle is None
    assert json.loads(lock_path.with_name("safari-context.lock.state").read_text())["state"] == "idle"


def test_safari_context_creates_a_standard_visible_background_window() -> None:
    context = SafariContext("https://grok.com/files")

    with patch("app.core.safari_automation.run_applescript", return_value="123") as run, patch.object(
        SafariPage,
        "goto",
    ) as goto:
        page = context._create_page("https://grok.com/files")

    script = run.call_args.args[0]
    assert run.call_args.kwargs == {"retry_transient": False}
    assert page.window_id == 123
    assert page.tab_index == 1
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
    assert "set targetDocument to make new document" in script
    assert "(document of candidateWindow) is targetDocument" in script
    assert "set targetWindow to candidateWindow" in script
    assert "existingWindowIds" not in script
    assert "emptyWindowIds" not in script
    assert "set visible of targetWindow to true" in script
    assert "set miniaturized of targetWindow to false" in script
    assert "targetWindowStillFront" in script
    assert "canRestorePreviousSafariWindow" not in script
    assert "set bounds of targetWindow" not in script
    assert 'Safari did not create an owned window.' in script
    assert "targetWindowStillFront and previousWindowId is not 0" in script
    assert script.index("set URL of current tab of targetWindow") < script.index(
        "set miniaturized of targetWindow to false"
    )


def test_safari_page_content_clips_source_after_reading_the_owned_tab() -> None:
    context = SafariContext("https://grok.com/files")
    page = SafariPage(context, window_id=123)

    with patch.object(page, "_run_in_window", return_value="0123456789") as run:
        assert page.content(limit=4) == "0123"

    assert run.call_args.args[0] == "return source of targetTab"


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
        return_value="https://chatgpt.com/project\nhttps://chatgpt.com/project\ninteractive",
    ) as run:
        state = page._read_navigation_state()

    assert state == {
        "href": "https://chatgpt.com/project",
        "nativeHref": "https://chatgpt.com/project",
        "readyState": "interactive",
    }
    assert "URL of targetTab" in run.call_args.args[0]
    assert "document.readyState" in run.call_args.args[0]
    assert "location.href" in run.call_args.args[0]
    assert "in targetTab" in run.call_args.args[0]


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
    assert "close targetWindow" in script
    assert "System Events" not in script
    assert "set index of targetWindow" not in script
    assert "if not (exists (first window whose id is 123))" in script
    assert 'return "closed"' in script
    assert "set URL of targetTab" not in script
    assert "close targetTab" not in script
    assert run.call_args.kwargs == {
        "recover_missing": False,
        "retry_transient": False,
        "bind_tab": False,
    }
    assert page._closed is True
    assert context.pages == []


def test_safari_window_only_operation_does_not_require_a_tab() -> None:
    """A residual zero-tab task window remains closable by its exact ID."""
    page = SafariPage(SafariContext("https://chatgpt.com/"), window_id=123)

    with patch(
        "app.core.safari_automation.run_applescript",
        return_value="closed",
    ) as run:
        result = page._run_in_window(
            "close targetWindow",
            bind_tab=False,
            retry_transient=False,
        )

    source = run.call_args.args[0]
    assert result == "closed"
    assert "first window whose id is 123" in source
    assert "targetTab" not in source
    assert "Safari target tab is missing" not in source
    assert run.call_args.kwargs == {"retry_transient": False}


def test_safari_context_serializes_concurrent_window_creation() -> None:
    contexts = [
        SafariContext("https://chatgpt.com/"),
        SafariContext("https://chatgpt.com/"),
    ]
    counter_lock = Lock()
    active_calls = 0
    maximum_active_calls = 0
    next_window_id = 100

    def create_window(_source: str, **_kwargs: object) -> str:
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


def test_safari_context_blocks_a_stale_owned_window_until_it_is_absent(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "owned",
                "ownership_token": "a" * 32,
                "owner_pid": 123,
                "baseline_windows": [],
                "window_id": 456,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://chatgpt.com/", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_pid_is_alive",
        return_value=True,
    ), patch(
        "app.core.safari_automation._close_safari_window_id",
        side_effect=AssertionError("A live foreign owner must not close the window"),
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        side_effect=({456: 3}, {}),
    ):
        with pytest.raises(RuntimeError, match="previous process"):
            context._acquire_context_lock()
        assert context._context_lock_handle is None

        context._acquire_context_lock()
        context._release_context_lock()

    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"


def test_safari_context_closes_leftover_window_when_owner_process_is_gone(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "owned",
                "ownership_token": "a" * 32,
                "owner_pid": 123,
                "baseline_windows": [],
                "window_id": 456,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://grok.com/files", lock_blocking=False)
    closed: list[int] = []

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_pid_is_alive",
        return_value=False,
    ), patch(
        "app.core.safari_automation._close_safari_window_id",
        side_effect=lambda window_id: closed.append(window_id) or True,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        side_effect=({456: 1}, {}),
    ):
        context._acquire_context_lock()
        context._release_context_lock()

    assert closed == [456]
    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"


def test_safari_context_closes_same_process_stale_owned_window(
    tmp_path: Path,
) -> None:
    import os

    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "owned",
                "ownership_token": "a" * 32,
                "owner_pid": os.getpid(),
                "baseline_windows": [],
                "window_id": 789,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://grok.com/files", lock_blocking=False)
    closed: list[int] = []

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._close_safari_window_id",
        side_effect=lambda window_id: closed.append(window_id) or True,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        side_effect=({789: 1}, {}),
    ):
        context._acquire_context_lock()
        context._release_context_lock()

    assert closed == [789]
    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"


def test_safari_context_adopts_leftover_window_when_close_fails(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "owned",
                "ownership_token": "a" * 32,
                "owner_pid": 123,
                "baseline_windows": [{"window_id": 10, "tab_count": 2}],
                "window_id": 456,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://grok.com/files", lock_blocking=False)
    closed: list[int] = []

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_pid_is_alive",
        return_value=False,
    ), patch(
        "app.core.safari_automation._close_safari_window_id",
        side_effect=lambda window_id: closed.append(window_id) or False,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        return_value={10: 2, 456: 1},
    ):
        context._acquire_context_lock()
        context._release_context_lock()

    assert closed == [456]
    assert context._adopted_window_id == 456
    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "owned"


def test_safari_context_clears_stale_lease_for_a_pre_existing_window(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "owned",
                "ownership_token": "a" * 32,
                "owner_pid": 123,
                "baseline_windows": [{"window_id": 456, "tab_count": 3}],
                "window_id": 456,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://grok.com/files", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._close_safari_window_id",
        side_effect=AssertionError("Daily Safari must not be closed"),
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        return_value={456: 3},
    ):
        context._acquire_context_lock()
        context._release_context_lock()

    assert context._adopted_window_id is None
    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"


def test_safari_context_reuses_an_adopted_window_instead_of_creating_another() -> None:
    context = SafariContext("https://grok.com/files")
    context._adopted_window_id = 456

    with patch(
        "app.core.safari_automation._safari_window_inventory",
        return_value={10: 2, 456: 1},
    ), patch.object(
        context,
        "_create_window",
        side_effect=AssertionError("must not create another Safari window"),
    ), patch.object(
        context,
        "_mark_context_window_owned",
    ) as mark, patch.object(
        SafariPage,
        "goto",
    ) as goto:
        page = context._create_page("https://grok.com/files")

    assert page.window_id == 456
    assert page.tab_index == 1
    assert context._adopted_window_id is None
    mark.assert_called_once_with(456)
    goto.assert_called_once_with(
        "https://grok.com/files",
        wait_until="domcontentloaded",
        timeout=60_000,
    )


def test_safari_context_migrates_a_legacy_clear_lease_without_inventory(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps({"version": 1, "state": "clear"}),
        encoding="utf-8",
    )
    context = SafariContext("https://chatgpt.com/", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
    ) as inventory:
        context._acquire_context_lock()
        context._release_context_lock()

    inventory.assert_not_called()
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": SAFARI_CONTEXT_LEASE_VERSION,
        "state": "clear",
    }


def test_safari_context_reconciles_a_legacy_owned_window_by_exact_id(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "state": "owned",
                "ownership_token": "a" * 32,
                "owner_pid": 123,
                "baseline_windows": [],
                "window_id": 456,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://chatgpt.com/", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_pid_is_alive",
        return_value=True,
    ), patch(
        "app.core.safari_automation._close_safari_window_id",
        side_effect=AssertionError("A live foreign owner must not close the window"),
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        side_effect=({10: 2, 456: 1}, {10: 2}),
    ):
        with pytest.raises(RuntimeError, match="previous process"):
            context._acquire_context_lock()
        context._acquire_context_lock()
        context._release_context_lock()

    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"


def test_safari_context_clears_malformed_owned_state_when_safari_has_no_windows(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "owned",
                "ownership_token": "a" * 32,
                "owner_pid": 123,
                "baseline_windows": [],
                "window_id": "not-an-int",
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://chatgpt.com/", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        return_value={},
    ):
        context._acquire_context_lock()
        context._release_context_lock()

    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"


def test_safari_context_recovers_non_utf8_state_only_without_safari_windows(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_bytes(b"\xff\xfe")
    context = SafariContext("https://chatgpt.com/", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        return_value={},
    ):
        context._acquire_context_lock()
        context._release_context_lock()

    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"


@pytest.mark.parametrize("raw_state", (b"", b"   \n", b"\xff\xfe", b"{invalid"))
def test_safari_context_keeps_unreadable_state_fail_closed_when_windows_exist(
    tmp_path: Path,
    raw_state: bytes,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_bytes(raw_state)
    context = SafariContext("https://chatgpt.com/", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        return_value={10: 1},
    ):
        with pytest.raises(RuntimeError, match="unreadable prior ownership"):
            context._acquire_context_lock()

    assert state_path.read_bytes() == raw_state


def test_safari_context_keeps_malformed_owned_state_fail_closed_when_windows_exist(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    payload = {
        "version": SAFARI_CONTEXT_LEASE_VERSION,
        "state": "owned",
        "ownership_token": "a" * 32,
        "owner_pid": 123,
        "baseline_windows": [],
        "window_id": "not-an-int",
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    context = SafariContext("https://chatgpt.com/", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        return_value={10: 1},
    ):
        with pytest.raises(RuntimeError, match="owned-window state is invalid"):
            context._acquire_context_lock()

    assert json.loads(state_path.read_text(encoding="utf-8")) == payload


def test_safari_context_blocks_an_interrupted_creation_until_inventory_recovers(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "creating",
                "ownership_token": "b" * 32,
                "owner_pid": 321,
                "baseline_windows": [{"window_id": 10, "tab_count": 0}],
                "creation_started_at_ns": 1,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://grok.com/", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        side_effect=({10: 1}, {10: 0}, {10: 0}),
    ), patch(
        "app.core.safari_automation.time.time_ns",
        return_value=int((SAFARI_CONTEXT_CREATION_SETTLE_SECONDS + 1) * 1_000_000_000),
    ), patch(
        "app.core.safari_automation.time.sleep",
    ):
        with pytest.raises(RuntimeError, match="creation was interrupted"):
            context._acquire_context_lock()
        context._acquire_context_lock()
        context._release_context_lock()

    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"


def test_safari_context_keeps_a_recent_interrupted_creation_fail_closed(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    creation_started_at_ns = 5_000_000_000
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "creating",
                "ownership_token": "e" * 32,
                "owner_pid": 321,
                "baseline_windows": [],
                "creation_started_at_ns": creation_started_at_ns,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://grok.com/", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation.time.time_ns",
        return_value=creation_started_at_ns + 1_000_000_000,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
    ) as inventory:
        with pytest.raises(RuntimeError, match="still verifying"):
            context._acquire_context_lock()

    inventory.assert_not_called()
    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "creating"


def test_safari_context_requires_two_safe_inventories_after_creation_settles(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "creating",
                "ownership_token": "f" * 32,
                "owner_pid": 321,
                "baseline_windows": [],
                "creation_started_at_ns": 1,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://grok.com/", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation.time.time_ns",
        return_value=int((SAFARI_CONTEXT_CREATION_SETTLE_SECONDS + 1) * 1_000_000_000),
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        side_effect=({}, {456: 1}),
    ), patch(
        "app.core.safari_automation.time.sleep",
    ) as sleep:
        with pytest.raises(RuntimeError, match="creation was interrupted"):
            context._acquire_context_lock()

    sleep.assert_called_once()
    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "creating"


def test_safari_context_rejects_a_future_creation_timestamp(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "creating",
                "ownership_token": "0" * 32,
                "owner_pid": 321,
                "baseline_windows": [],
                "creation_started_at_ns": 10_000_000_000,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://grok.com/", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation.time.time_ns",
        return_value=5_000_000_000,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
    ) as inventory:
        with pytest.raises(RuntimeError, match="timing state is invalid"):
            context._acquire_context_lock()

    inventory.assert_not_called()
    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "creating"


def test_safari_context_ignores_user_tab_changes_in_a_nonempty_baseline_window(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(
        json.dumps(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "creating",
                "ownership_token": "d" * 32,
                "owner_pid": 654,
                "baseline_windows": [{"window_id": 10, "tab_count": 2}],
                "creation_started_at_ns": 1,
            }
        ),
        encoding="utf-8",
    )
    context = SafariContext("https://chatgpt.com/", lock_blocking=False)

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation._safari_window_inventory",
        side_effect=({10: 2}, {10: 3}),
    ), patch(
        "app.core.safari_automation.time.time_ns",
        return_value=int((SAFARI_CONTEXT_CREATION_SETTLE_SECONDS + 1) * 1_000_000_000),
    ), patch(
        "app.core.safari_automation.time.sleep",
    ):
        context._acquire_context_lock()
        context._release_context_lock()

    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"


def test_safari_context_tracks_a_window_left_open_during_initial_creation_failure(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    context = SafariContext("https://chatgpt.com/")
    creation_error = RuntimeError(
        "SAFARI_OWNED_WINDOW_REMAINS:456:simulated create failure (-1719)"
    )
    close_error = RuntimeError("Safari window 456 remained open.")

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation.run_applescript",
        side_effect=("windows:10:1", creation_error),
    ), patch(
        "app.core.safari_automation._safari_process_identity",
        return_value="test-safari-session",
    ), patch.object(
        SafariPage,
        "_close_owned_window",
        side_effect=(close_error, None),
    ):
        with pytest.raises(RuntimeError, match="could not verify cleanup"):
            context.__enter__()

        assert context._context_lock_handle is not None
        assert [page.window_id for page in context.pages] == [456]
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["state"] == "owned"
        assert persisted["window_id"] == 456
        assert retry_pending_safari_context_cleanup() == (1, 0)

    assert context._context_lock_handle is None
    assert context.pages == []
    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"


def test_safari_context_non_numeric_window_result_remains_fail_closed(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    context = SafariContext("https://gemini.google.com/app")
    clock_ns = [1_000_000_000]

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation.run_applescript",
        side_effect=(
            "windows:10:0",
            "not-a-window-id",
            "windows:10:0",
            "windows:10:0",
        ),
    ), patch(
        "app.core.safari_automation.time.time_ns",
        side_effect=lambda: clock_ns[0],
    ), patch(
        "app.core.safari_automation.time.sleep",
    ):
        with pytest.raises(RuntimeError, match="could not verify cleanup"):
            context.__enter__()
        assert context._context_lock_handle is not None
        assert context.pages == []
        assert retry_pending_safari_context_cleanup() == (1, 1)
        clock_ns[0] += int(
            (SAFARI_CONTEXT_CREATION_SETTLE_SECONDS + 1) * 1_000_000_000
        )
        assert retry_pending_safari_context_cleanup() == (1, 0)

    assert context._context_lock_handle is None
    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"


def test_safari_context_unknown_creation_timeout_remains_fail_closed(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    context = SafariContext("https://grok.com/")
    clock_ns = [1_000_000_000]

    with patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation.run_applescript",
        side_effect=(
            "windows:10:0",
            RuntimeError("Safari automation timed out after 20 seconds."),
            "windows:10:0\n456:1",
            "windows:10:0",
            "windows:10:0",
        ),
    ), patch(
        "app.core.safari_automation.time.time_ns",
        side_effect=lambda: clock_ns[0],
    ), patch(
        "app.core.safari_automation.time.sleep",
    ):
        with pytest.raises(RuntimeError, match="could not verify cleanup"):
            context.__enter__()
        assert context._context_lock_handle is not None
        assert context.pages == []
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["state"] == "creating"
        assert retry_pending_safari_context_cleanup() == (1, 1)
        clock_ns[0] += int(
            (SAFARI_CONTEXT_CREATION_SETTLE_SECONDS + 1) * 1_000_000_000
        )
        assert retry_pending_safari_context_cleanup() == (1, 1)
        assert retry_pending_safari_context_cleanup() == (1, 0)

    assert context._context_lock_handle is None
    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == "clear"


def test_safari_context_lease_replacement_failure_preserves_prior_state(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    prior_state = {
        "version": 1,
        "state": "owned",
        "ownership_token": "c" * 32,
        "owner_pid": 123,
        "baseline_windows": [],
        "window_id": 456,
    }
    state_path.write_text(json.dumps(prior_state), encoding="utf-8")
    context = SafariContext("https://chatgpt.com/")

    with lock_path.open("a+") as handle, patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
        lock_path,
    ), patch(
        "app.core.safari_automation.os.replace",
        side_effect=OSError("simulated atomic replacement failure"),
    ):
        context._context_lock_handle = handle
        with pytest.raises(RuntimeError, match="could not be saved"):
            context._write_context_lease_state(
                {"version": 1, "state": "clear"}
            )
        context._context_lock_handle = None

    assert json.loads(state_path.read_text(encoding="utf-8")) == prior_state
    assert list(tmp_path.glob(".safari-context.lock.state.*.tmp")) == []


def test_safari_context_adds_additional_pages_as_tabs_in_the_owned_window() -> None:
    context = SafariContext("https://chatgpt.com/")
    first = SafariPage(context, window_id=123, tab_index=1)
    context.pages.append(first)

    with patch(
        "app.core.safari_automation.run_applescript",
        return_value="2",
    ) as run, patch.object(SafariPage, "goto") as goto:
        page = context._create_page("about:blank")

    script = run.call_args.args[0]
    assert run.call_args.kwargs == {"retry_transient": False}
    assert page.window_id == 123
    assert page.tab_index == 2
    assert "make new tab at end of tabs of targetWindow" in script
    assert "make new document" not in script
    assert "frontmost of process previousFrontmostProcessName" in script
    assert "targetWindowStillFront" in script
    assert "canRestorePreviousSafariWindow" not in script
    assert "set current tab of targetWindow to newTab" in script
    goto.assert_called_once_with(
        "about:blank",
        wait_until="domcontentloaded",
        timeout=60_000,
    )


def test_safari_context_does_not_replay_an_uncertain_tab_creation() -> None:
    context = SafariContext("https://chatgpt.com/")
    first = SafariPage(context, window_id=123, tab_index=1)
    context.pages.append(first)

    with patch(
        "app.core.safari_automation.run_applescript",
        side_effect=RuntimeError("Safari automation timed out after 20 seconds."),
    ) as run:
        with pytest.raises(RuntimeError, match="timed out"):
            context._create_page("about:blank")

    assert run.call_count == 1
    assert run.call_args.kwargs == {"retry_transient": False}
    assert context.pages == [first]


def test_safari_page_closes_a_sibling_tab_without_closing_the_window() -> None:
    context = SafariContext("https://chatgpt.com/")
    first = SafariPage(context, window_id=123, tab_index=1)
    second = SafariPage(context, window_id=123, tab_index=2)
    third = SafariPage(context, window_id=123, tab_index=3)
    context.pages.extend([first, second, third])

    with patch.object(second, "_run_in_window", return_value="closed") as run:
        second.close()

    script = run.call_args.args[0]
    assert "close targetTab" in script
    assert "click button 1 of front window" not in script
    assert second._closed is True
    assert second not in context.pages
    assert first.tab_index == 1
    assert third.tab_index == 2
    assert [page.tab_index for page in context.pages] == [1, 2]


def test_safari_page_evaluate_binds_the_owned_tab() -> None:
    page = SafariPage(
        SafariContext("https://chatgpt.com/"),
        window_id=123,
        tab_index=2,
    )

    with patch(
        "app.core.safari_automation.run_applescript",
        return_value='{"ok":true,"value":1}',
    ) as run:
        assert page.evaluate("() => 1") == 1

    script = run.call_args.args[0]
    assert "set targetTab to tab 2 of targetWindow" in script
    assert "set current tab of targetWindow to targetTab" in script
    assert "delay 0.05" in script
    assert "in targetTab" in script.split("do JavaScript", 1)[1]


def test_safari_page_evaluate_retries_an_unreadable_background_tab_result() -> None:
    page = SafariPage(SafariContext("https://chatgpt.com/"), window_id=123, tab_index=1)
    encoded = '{"ok":true,"value":"ready"}'
    scripts: list[str] = []
    evaluate_attempts = 0

    def run_window(statement: str, **_kwargs: object) -> str:
        scripts.append(statement)
        if "return JSON.stringify({ok:true,value})" in statement:
            nonlocal evaluate_attempts
            evaluate_attempts += 1
            if evaluate_attempts == 1:
                return "missing value"
            return encoded
        return ""

    with patch.object(page, "_run_in_window", side_effect=run_window), patch(
        "app.core.safari_automation.time.sleep"
    ):
        assert page.evaluate("() => 'ready'") == "ready"

    assert evaluate_attempts == 2
    background_scripts = [
        script
        for script in scripts
        if "set targetWindowStillFront" in script
        and "set miniaturized of targetWindow to false" in script
    ]
    assert len(background_scripts) == 1
    assert not any("nativeFocusReady" in script for script in scripts)
    assert not any("\nactivate\n" in script for script in scripts)
    assert page._native_input_transaction_depth == 0


def test_safari_page_evaluate_uses_a_restorable_wake_only_after_background_retry() -> None:
    page = SafariPage(SafariContext("https://chatgpt.com/"), window_id=123, tab_index=1)
    encoded = '{"ok":true,"value":"ready"}'
    events: list[str] = []
    evaluate_attempts = 0

    def run_window(statement: str, **_kwargs: object) -> str:
        nonlocal evaluate_attempts
        if "return previousFrontmostProcessName & linefeed" in statement:
            events.append("capture")
            return "Codex\n1\ntrue\nfalse"
        if "nativeFocusReady" in statement:
            events.append("wake")
            return ""
        if 'set previousFrontmostProcessName to "Codex"' in statement:
            events.append("restore")
            return ""
        if "set miniaturized of targetWindow to false" in statement:
            events.append("keep")
            return ""
        if "return JSON.stringify({ok:true,value})" in statement:
            evaluate_attempts += 1
            events.append("evaluate")
            if evaluate_attempts < 3:
                return "missing value"
            return encoded
        return ""

    with patch.object(page, "_run_in_window", side_effect=run_window), patch(
        "app.core.safari_automation.time.sleep"
    ):
        assert page.evaluate("() => 'ready'") == "ready"

    assert evaluate_attempts == 3
    assert events == [
        "evaluate",
        "keep",
        "evaluate",
        "capture",
        "wake",
        "restore",
        "evaluate",
    ]
    assert page._native_input_transaction_depth == 0


def test_safari_page_evaluate_stages_a_large_argument_in_chunks() -> None:
    page = SafariPage(SafariContext("https://chatgpt.com/"), window_id=123, tab_index=1)
    scripts: list[str] = []

    def run_window(statement: str, **_kwargs: object) -> str:
        scripts.append(statement)
        if "JSON.parse(window.__cachelikesSafariEvalArg" in statement:
            return '{"ok":true,"value":"ok"}'
        return '{"ok":true,"value":null}'

    with patch.object(page, "_run_in_window", side_effect=run_window):
        assert page.evaluate("({value}) => value.slice(0, 2)", {"value": "x" * 2500}) == "ok"

    assert any("window.__cachelikesSafariEvalArg[token] = ''" in script for script in scripts)
    assert sum("+= chunk" in script for script in scripts) >= 2
    assert any("JSON.parse(window.__cachelikesSafariEvalArg" in script for script in scripts)
    javascript_literals = [
        script.split("do JavaScript ", 1)[1].split(" in current tab", 1)[0]
        for script in scripts
        if "do JavaScript " in script
    ]
    assert javascript_literals
    assert all("\n" not in literal for literal in javascript_literals)
