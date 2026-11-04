"""Microsoft frontend-derived Copilot catalog transport and loader contracts."""

# Code version: v1.2.0-codex.0

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from playwright.sync_api import Error as PlaywrightError

import test_sidebar_e2e
from app.core import copilot_history as history
from app.core.safari_automation import SafariResponse


disposable_browser = test_sidebar_e2e.disposable_browser


@pytest.fixture
def real_catalog_response(disposable_browser):
    """Return real response interfaces using only an isolated local fixture endpoint."""

    class FixtureHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, _format, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), FixtureHandler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    context = disposable_browser.new_context()
    worker.start()

    def response_for(engine, payload):
        response = context.request.post(
            f"http://127.0.0.1:{server.server_port}/fixture",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        if engine == "safari":
            return SafariResponse(response.status, response.text(), response.headers)
        return response

    try:
        yield response_for
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def _catalog(start=0, stop=17, cursor="cursor-a"):
    return {
        "chats": [
            {"conversationId": f"session-{index}", "chatName": f"Fixture session {index}"}
            for index in range(start, stop)
        ],
        "syncState": cursor,
    }


def _transport(engine, *, payload=None, status=200, url=history.COPILOT_HISTORY_URL, body_text=None):
    serialized = json.dumps(payload) if body_text is None else body_text
    if engine == "safari":
        response = SafariResponse(status, serialized)
        request = SimpleNamespace(
            request_from_page=Mock(return_value=response),
            post=Mock(side_effect=AssertionError("Safari history reads must use its owned page transport.")),
        )
    else:
        response = SimpleNamespace(status=status, text=Mock(return_value=serialized))
        request = SimpleNamespace(post=Mock(return_value=response))
    page = SimpleNamespace(url=url, context=SimpleNamespace(request=request))
    return page, request, response


@pytest.mark.parametrize("engine", ["safari", "chromium"])
@pytest.mark.parametrize("query", ["", "es=Click&fromcode=fixture%2Bvalue&repeat=one&repeat=two"])
def test_catalog_transport_uses_read_action_preserves_query_and_retains_previous_state(engine, query):
    previous = _catalog()
    before = deepcopy(previous)
    next_catalog = _catalog(17, 34, "")
    page_url = history.COPILOT_HISTORY_URL + (f"?{query}" if query else "")
    page, request, _response = _transport(
        engine, payload={"store": {"chatLandingPageHistoryList": next_catalog}}, url=page_url,
    )
    result = history.fetch_copilot_catalog_page(page, previous, "opaque+/cursor==", "fixture-session")
    if engine == "safari":
        request.request_from_page.assert_called_once()
        actual_page, url, timeout, headers = request.request_from_page.call_args.args
        options = request.request_from_page.call_args.kwargs
        assert actual_page is page
        assert options["method"] == "POST"
        body = options["body"]
        request.post.assert_not_called()
    else:
        request.post.assert_called_once()
        (url,) = request.post.call_args.args
        options = request.post.call_args.kwargs
        timeout = options["timeout"]
        headers = options["headers"]
        body = options["data"]
        assert options["max_redirects"] == 0
        assert headers["Origin"] == "https://copilot.com"
    assert url == "https://copilot.com/chat" + (f"?{query}" if query else "")
    assert timeout == 45_000
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Route-Id"] == "chat"
    assert headers["X-Session-Id"] == "fixture-session"
    assert json.loads(body) == {
        "action": "GetConversationPageHistoryList",
        "syncState": "opaque+/cursor==",
        "enableLastMessage": True,
        "state": {"chatLandingPageHistoryList": previous},
        "saveToStore": "chatLandingPageHistoryList",
    }
    assert previous == before
    assert result == next_catalog


@pytest.mark.parametrize("engine", ["safari", "chromium"])
def test_empty_session_identifier_is_not_fabricated_into_a_header(engine):
    page, request, _response = _transport(engine, payload={"store": {"chatLandingPageHistoryList": _catalog(cursor="")}})
    history.fetch_copilot_catalog_page(page, _catalog(), "cursor-a", "")
    headers = request.request_from_page.call_args.args[3] if engine == "safari" else request.post.call_args.kwargs["headers"]
    assert "X-Session-Id" not in headers


@pytest.mark.parametrize("engine", ["safari", "chromium"])
def test_only_selected_public_context_becomes_official_headers(engine):
    catalog = _catalog(cursor="")
    page, request, _response = _transport(engine, payload={"store": {"chatLandingPageHistoryList": catalog}})
    request_context = {
        "eligibility": {"cohort": "Consumer", "Token": 5, "tokenQuota": 1_000, "isAuthEnabled": True},
        "hostContext": {"surface": "web", "hostName": "Copilot 研究 😀"},
        "sessionID": "merged-session",
        "Authorization": "PRIVATE-FIXTURE",
        "Cookie": "PRIVATE-FIXTURE",
        "arbitraryHeader": "PRIVATE-FIXTURE",
    }
    before = deepcopy(request_context)
    previous = _catalog()
    assert history.fetch_copilot_catalog_page(
        page, previous, "cursor-a", "stale-root-session", request_context=request_context,
    ) == catalog
    options = request.request_from_page.call_args.kwargs if engine == "safari" else request.post.call_args.kwargs
    headers = request.request_from_page.call_args.args[3] if engine == "safari" else options["headers"]
    assert headers["X-Session-Id"] == "merged-session"
    assert headers["X-Client-Eligibility"] == json.dumps(request_context["eligibility"], ensure_ascii=False, separators=(",", ":"))
    assert headers["x-host-context"] == json.dumps(request_context["hostContext"], ensure_ascii=True, separators=(",", ":"))
    assert headers["x-host-context"].isascii()
    assert "PRIVATE-FIXTURE" not in json.dumps(headers)
    assert not {"authorization", "cookie", "arbitraryheader"} & {name.lower() for name in headers}
    body = json.loads(options["body"] if engine == "safari" else options["data"])
    assert body["state"] == {"chatLandingPageHistoryList": previous}
    assert "request_context" not in body
    assert request_context == before


@pytest.mark.parametrize("engine", ["safari", "chromium"])
def test_explicit_null_context_clears_old_session_and_omits_optional_headers(engine):
    page, request, _response = _transport(engine, payload={"store": {"chatLandingPageHistoryList": _catalog(cursor="")}})
    history.fetch_copilot_catalog_page(
        page, _catalog(), "cursor-a", "stale-session",
        request_context={"eligibility": None, "hostContext": None, "sessionID": None},
    )
    headers = request.request_from_page.call_args.args[3] if engine == "safari" else request.post.call_args.kwargs["headers"]
    assert not {"X-Session-Id", "X-Client-Eligibility", "x-host-context"} & set(headers)


@pytest.mark.parametrize("engine", ["safari", "chromium"])
@pytest.mark.parametrize("credential", ["Authorization", "Cookie", "access_token", "accessToken", "refresh_token", "refreshToken", "id_token", "idToken"])
def test_selected_context_rejects_exact_nested_credential_keys_without_requesting(engine, credential):
    page, request, _response = _transport(engine)
    with pytest.raises(RuntimeError, match="unsupported history request context") as error:
        history.fetch_copilot_catalog_page(
            page, _catalog(), "cursor-a", "fixture-session",
            request_context={"eligibility": {"nested": [{credential: "PRIVATE-FIXTURE"}]}},
        )
    assert "PRIVATE-FIXTURE" not in str(error.value)
    request.post.assert_not_called()
    if engine == "safari":
        request.request_from_page.assert_not_called()


def test_www_origin_is_retained_for_its_own_authenticated_context():
    page, request, _response = _transport(
        "chromium",
        payload={"store": {"chatLandingPageHistoryList": _catalog(cursor="")}},
        url="https://www.copilot.com/chat/all?fromcode=fixture",
    )
    history.fetch_copilot_catalog_page(page, _catalog(), "cursor-a", "fixture-session")
    assert request.post.call_args.args[0] == "https://www.copilot.com/chat?fromcode=fixture"
    assert request.post.call_args.kwargs["headers"]["Origin"] == "https://www.copilot.com"


@pytest.mark.parametrize("status", [201, 204, 301, 400, 401, 403, 500])
@pytest.mark.parametrize("engine", ["safari", "chromium"])
def test_non_200_history_response_is_rejected_before_json_decoding(status, engine):
    page, _request, response = _transport(engine, status=status, body_text="<html>Fixture HTTP error.</html>")
    with pytest.raises(RuntimeError, match="HTTP"):
        history.fetch_copilot_catalog_page(page, _catalog(), "cursor-a", "fixture-session")
    if engine == "chromium":
        response.text.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"chatLandingPageHistoryList": _catalog(cursor="")},
        {"data": {"store": {"chatLandingPageHistoryList": _catalog(cursor="")}}},
        {"store": None},
        {"store": {"chatLandingPageHistoryList": []}},
        {"store": {"chatLandingPageHistoryList": _catalog(cursor="")}, "error": True},
        {"store": {"chatLandingPageHistoryList": _catalog(cursor="")}, "error": {"message": "Fixture provider error."}},
    ],
)
def test_unknown_or_error_response_wrapper_is_never_accepted_as_a_catalog(payload):
    page, _request, _response = _transport("chromium", payload=payload)
    with pytest.raises(RuntimeError, match="unsupported|history"):
        history.fetch_copilot_catalog_page(page, _catalog(), "cursor-a", "fixture-session")


@pytest.mark.parametrize("engine", ["safari", "chromium"])
@pytest.mark.parametrize("redirect_to", ["/chat", "https://login.live.com/", "https://copilot.com/chat/all"])
def test_redirect_action_cannot_establish_catalog_completion(engine, redirect_to, real_catalog_response):
    previous = _catalog()
    before = deepcopy(previous)
    response = real_catalog_response(engine, {
        "store": {"chatLandingPageHistoryList": _catalog(17, 34, "")},
        "redirectTo": redirect_to,
    })
    page, request, _response = _transport(engine)
    if engine == "safari":
        request.request_from_page.return_value = response
    else:
        request.post.return_value = response
    with pytest.raises(history.CopilotDiscoveryIncompleteError) as incomplete:
        history.traverse_copilot_catalog(
            previous,
            lambda cursor: history.fetch_copilot_catalog_page(page, previous, cursor, "fixture-session"),
            lambda: False,
        )
    assert len(incomplete.value.conversations) == 17
    assert previous == before
    assert "redirected its history action" in str(incomplete.value.__cause__)
    assert redirect_to not in str(incomplete.value.__cause__)


@pytest.mark.parametrize("engine", ["safari", "chromium"])
def test_empty_redirect_field_keeps_a_valid_terminal_catalog(engine, real_catalog_response):
    catalog = _catalog(17, 34, "")
    response = real_catalog_response(engine, {
        "store": {"chatLandingPageHistoryList": catalog},
        "redirectTo": "",
    })
    page, request, _response = _transport(engine)
    if engine == "safari":
        request.request_from_page.return_value = response
    else:
        request.post.return_value = response
    assert history.fetch_copilot_catalog_page(page, _catalog(), "cursor-a", "fixture-session") == catalog


@pytest.mark.parametrize("engine", ["safari", "chromium"])
@pytest.mark.parametrize("body", ["<html>Fixture response was HTML.</html>", ""])
def test_invalid_json_response_cannot_be_used_as_catalog_data(engine, body):
    page, _request, _response = _transport(engine, body_text=body)
    with pytest.raises(RuntimeError, match="invalid history-action"):
        history.fetch_copilot_catalog_page(page, _catalog(), "cursor-a", "fixture-session")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/chat/all",
        "https://copilot.com.evil.example/chat/all",
        "http://copilot.com/chat/all",
        "https://login.live.com/",
        "https://copilot.com/chat",
        "https://copilot.com/chat/conversation/session-1",
    ],
)
def test_transport_requires_the_authenticated_first_party_all_chats_surface(url):
    page, request, _response = _transport("chromium", url=url)
    with pytest.raises(history.CopilotAccessError):
        history.fetch_copilot_catalog_page(page, _catalog(), "cursor-a", "fixture-session")
    request.post.assert_not_called()


@pytest.mark.parametrize(
    "path, search, grid, root, hydration, expected",
    [
        ("/chat/all", True, True, True, True, True),
        ("/chat", True, True, True, True, False),
        ("/chat/all", False, True, True, True, False),
        ("/chat/all", True, False, True, True, False),
        ("/chat/all", True, True, False, True, False),
        ("/chat/all", True, True, True, False, False),
    ],
)
def test_initial_catalog_script_requires_observed_route_root_and_loader(
    disposable_browser, path, search, grid, root, hydration, expected
):
    context = disposable_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    try:
        catalog = _catalog()
        loader = {"loaderData": {
            "root": {"store": {"sessionID": "fixture-session"}},
            "all-chat-history": {"store": {"chatLandingPageHistoryList": catalog}},
        }}
        markup = '<input data-testid="chat-history-search-input">' if search else ""
        markup += '<div role="grid"><div role="row">Fixture history card</div></div>' if grid else ""
        identity = "llm-web-ui-messageList-scrollable-container" if root else "unrelated-scroll-owner"
        document = f'<aside><a href="https://copilot.com/chat/conversation/sidebar-only">Recent chat</a></aside><section id="{identity}">{markup}</section>'
        if hydration:
            document += f"<script>window.__staticRouterHydrationData = {json.dumps(loader)};</script>"
        url = f"https://copilot.com{path}"
        page.route(url, lambda route: route.fulfill(content_type="text/html", body=document))
        page.goto(url)
        snapshot = page.evaluate(history.COPILOT_CATALOG_SCRIPT)
        assert snapshot.get("ready") is expected
        if expected:
            assert snapshot["catalog"] == catalog
            assert snapshot["session_id"] == "fixture-session"
    finally:
        context.close()


def test_discovery_waits_for_real_catalog_then_traverses_without_reading_sidebar():
    initial = _catalog()
    request_context = {"eligibility": {"cohort": "Consumer"}, "hostContext": {"surface": "web"}, "sessionID": "fixture-session"}

    class Page:
        url = history.COPILOT_HOME_URL

        def __init__(self):
            self.catalog_reads = 0
            self.waits = 0

        def goto(self, url, **_kwargs):
            self.url = url

        def wait_for_timeout(self, _milliseconds):
            self.waits += 1

        def evaluate(self, script):
            if script == history.COPILOT_STATE_SCRIPT:
                return {"signed_in": True, "loading": False}
            if script == history.COPILOT_CATALOG_SCRIPT:
                self.catalog_reads += 1
                return {"ready": False} if self.catalog_reads == 1 else {
                    "ready": True, "catalog": initial, "session_id": "fixture-session",
                    "request_context": request_context,
                }
            raise AssertionError("Discovery must not read sidebar links or use DOM scroll exhaustion.")

    page = Page()
    progress = []
    with patch.object(history, "fetch_copilot_catalog_page", return_value=_catalog(17, 34, "")) as fetch:
        result = history.discover_copilot_conversations(page, lambda: False, progress.append)
    assert result.discovery_complete
    assert result.expected_sessions is None
    assert len(result.conversations) == 34
    assert progress == [17, 34]
    assert page.waits == 1
    fetch.assert_called_once_with(page, initial, "cursor-a", "fixture-session", request_context=request_context)


def _context_snapshot(browser, loader, *, mutate=None, compact=False):
    context = browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    try:
        loader = deepcopy(loader)
        loader.setdefault("all-chat-history", {}).setdefault("store", {})["chatLandingPageHistoryList"] = _catalog()
        document = '''<section id="llm-web-ui-messageList-scrollable-container">
          <input data-testid="chat-history-search-input"><div role="grid"><div role="row">Fixture card</div></div></section>'''
        document += f"<script>window.__staticRouterHydrationData = {{loaderData: {json.dumps(loader)}}};</script>"
        page.route(history.COPILOT_HISTORY_URL, lambda route: route.fulfill(content_type="text/html", body=document))
        page.goto(history.COPILOT_HISTORY_URL)
        if mutate:
            page.evaluate(mutate)
        script = " ".join(history.COPILOT_CATALOG_SCRIPT.split()) if compact else history.COPILOT_CATALOG_SCRIPT
        return page.evaluate(script)
    finally:
        context.close()


def _query(updated_at, data, *, status="success", mirror="storePatch"):
    return {"state": {"status": status, "data": data, "dataUpdatedAt": updated_at}, "meta": {"storeMirror": {"type": mirror}}}


def test_catalog_context_merges_routes_then_latest_successful_query_per_field(disposable_browser):
    loader = {
        "root": {
            "store": {"eligibility": {"cohort": "root"}, "hostContext": {"surface": "root"}, "sessionID": "root-session", "Authorization": "PRIVATE-FIXTURE", "identity": {"accessToken": "PRIVATE-FIXTURE"}},
            "__queryState": {"queries": [
                _query(300, {"sessionID": "query-session", "Authorization": "PRIVATE-FIXTURE"}),
                _query(200, {"eligibility": {"cohort": "latest", "Token": 7, "tokenQuota": 1_000}}),
                _query(150, {"hostContext": {"surface": "query"}}),
                _query(50, {"eligibility": {"cohort": "stale"}, "hostContext": {"surface": "stale"}, "sessionID": "stale-session"}),
                _query(1_000, {"eligibility": {"cohort": "failed"}}, status="error"),
                _query(2_000, {"sessionID": "non-mirrored"}, mirror="none"),
            ]},
        },
        "chat": {"store": {"eligibility": {"cohort": "route"}, "hostContext": {"surface": "route"}, "sessionID": "route-session", "Cookie": "PRIVATE-FIXTURE"}},
    }
    snapshot = _context_snapshot(disposable_browser, loader, compact=True)
    assert snapshot["request_context"] == {
        "eligibility": {"cohort": "latest", "Token": 7, "tokenQuota": 1_000},
        "hostContext": {"surface": "query"},
        "sessionID": "query-session",
    }
    assert snapshot["session_id"] == "query-session"
    assert "PRIVATE-FIXTURE" not in json.dumps(snapshot)


def test_route_context_defined_values_override_earlier_routes_without_copying_other_state(disposable_browser):
    snapshot = _context_snapshot(disposable_browser, {
        "root": {"store": {"eligibility": {"cohort": "root"}, "sessionID": "root-session"}},
        "chat": {"store": {"eligibility": {"cohort": "chat"}, "hostContext": {"surface": "web"}, "sessionID": "chat-session", "arbitrary": "PRIVATE-FIXTURE"}},
    })
    assert snapshot["request_context"] == {"eligibility": {"cohort": "chat"}, "hostContext": {"surface": "web"}, "sessionID": "chat-session"}
    assert "PRIVATE-FIXTURE" not in json.dumps(snapshot)


@pytest.mark.parametrize("route_value,query_value", [("undefined", "undefined"), ("null", "undefined"), ("undefined", "null"), ("null", "null")])
def test_context_undefined_does_not_overwrite_but_null_remains_explicit(disposable_browser, route_value, query_value):
    original = {"eligibility": {"cohort": "root"}, "hostContext": {"surface": "web"}, "sessionID": "root-session"}
    loader = {"root": {"store": original, "__queryState": {"queries": [_query(100, {})]}}, "chat": {"store": {}}}
    mutate = f'''() => {{
        const loader = window.__staticRouterHydrationData.loaderData;
        for (const field of ['eligibility', 'hostContext', 'sessionID']) {{
            loader.chat.store[field] = {route_value};
            loader.root.__queryState.queries[0].state.data[field] = {query_value};
        }}
    }}'''
    snapshot = _context_snapshot(disposable_browser, loader, mutate=mutate)
    expected = original if route_value == query_value == "undefined" else dict.fromkeys(original)
    assert snapshot["request_context"] == expected
    assert snapshot["session_id"] == ("root-session" if expected["sessionID"] else "")


@pytest.mark.parametrize("root_present", [False, True])
def test_query_context_uses_hwav2_only_when_root_loader_is_absent(disposable_browser, root_present):
    loader = {"hwav2": {"store": {"sessionID": "hwav2-store"}, "__queryState": {"queries": [_query(100, {"sessionID": "hwav2-query"})]}}}
    if root_present:
        loader = {"root": {"store": {"sessionID": "root-store"}}, **loader}
    snapshot = _context_snapshot(disposable_browser, loader)
    assert snapshot["session_id"] == ("hwav2-store" if root_present else "hwav2-query")


@pytest.mark.parametrize("field,credential", [("eligibility", "accessToken"), ("hostContext", "Cookie")])
def test_catalog_script_rejects_credentials_inside_selected_context_before_returning(disposable_browser, field, credential):
    with pytest.raises(PlaywrightError, match="credentials in its history request context") as error:
        _context_snapshot(disposable_browser, {"root": {"store": {field: {"nested": [{credential: "PRIVATE-FIXTURE"}]}}}})
    assert "PRIVATE-FIXTURE" not in str(error.value)
