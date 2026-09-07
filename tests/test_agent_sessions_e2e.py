"""Session switching, capacity, and selected controls. Code version: v1.0.6-codex.1."""

from copy import deepcopy

import pytest
from playwright.sync_api import expect

from tests import test_sidebar_e2e as fixtures

disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url


@pytest.mark.parametrize("width", [1138, 390])
def test_switch_sessions_and_stop_only_selected(disposable_browser, sidebar_server_url, width):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    agents = {}
    for key, revision in (("primary", 102), ("second", 101)):
        agents[key] = deepcopy(base["agent"])
        agents[key].update(session_id=key, running=True, phase="running", run_id=key, run_revision=revision,
            session_title=f"Task {key}", prompt=f"Prompt {key}", response=f"Answer {key}", response_html=f"<p>Answer {key}</p>",
            history=[], activity=[{"status": "running", "label": "Read", "detail": key}])
    stopped = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        route.fulfill(json={**base, "agent": agents.get(key, {"session_id": "new"}),
            "sessions": [dict(agent) for agent in agents.values()],
            "active_count": sum(agent["running"] for agent in agents.values()), "concurrency_limit": 2})

    def stop(route):
        key = route.request.headers["x-cachelikes-agent-session"]
        stopped.append(key)
        agents[key].update(running=False, phase="finished")
        route.fulfill(json={**base, "agent": agents[key], "stop_requested": True})

    page.route("**/api/agent/status", status)
    page.route("**/api/agent/stop", stop)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions(),
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        rail = page.locator("[data-agent-execution-sessions]")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        expect(rail).to_be_visible()
        expect(rail).to_contain_text("2 of 2 active")
        row = rail.locator("[data-execution-session-id=primary]")
        expect(row).to_have_css("border-radius", "999px")
        assert abs(row.bounding_box()["height"] - 36) < 0.1
        assert rail.evaluate("e => Boolean(document.querySelector('[data-agent-project-session-field]').compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING)")
        spinner = rail.locator(".suggestion-loading-spinner").first
        expect(spinner).to_have_attribute("aria-label", "Running")
        expect(spinner).to_have_text("")
        assert spinner.evaluate("e => getComputedStyle(e).maskImage.includes('loading.spinner.svg')")
        assert spinner.evaluate("e => getComputedStyle(e).animationName") == "ticker-suggestion-loading"

        expect(page.locator("#agent_response_question")).to_have_text("Prompt primary")
        page.locator("[data-execution-session-id=second]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Prompt second")
        page.reload()
        expect(page.locator("#agent_response_question")).to_have_text("Prompt second")
        expect(page.locator("[data-execution-session-id=second]")).to_have_attribute("aria-pressed", "true")
        if width < 900 and not rail.is_visible():
            page.locator("#sidebar_toggle").click()
        expect(page.locator("#agent_activity_list")).to_contain_text("second")
        expect(page.locator("[data-execution-session-id=second]")).to_have_attribute("aria-pressed", "true")
        page.locator("[data-agent-new-task]").click()
        expect(page.locator("[data-agent-session-capacity]")).to_have_text("· 2 of 2 active")
        expect(page.locator("#agent_response_output")).to_be_hidden()
        ask = page.get_by_role("button", name="Ask ChatGPT Web", exact=True)
        expect(ask).to_be_disabled()
        prompt = page.locator("#agent_prompt_input")
        prompt.fill("A separate draft")
        page.locator("[data-execution-session-id=second]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Prompt second")
        page.locator("[data-agent-new-task]").click()
        expect(prompt).to_have_value("A separate draft")
        page.locator("[data-execution-session-id=second]").click()
        if width < 900:
            page.locator("#sidebar_toggle").click()
        page.get_by_role("button", name="Stop Agent task", exact=True).click()
        expect(page.locator("[data-agent-session-capacity]")).to_have_text("· 1 of 2 active")
        assert stopped == ["second"]
        assert agents["primary"]["running"]
        if width < 900:
            page.locator("#sidebar_toggle").click()
        geometry = rail.evaluate("e => ({width: e.clientWidth, scroll: e.scrollWidth})")
        assert geometry["scroll"] <= geometry["width"] + 1
        page.locator("[data-agent-new-task]").click()
        expect(ask).to_be_enabled()
        assert not errors
    finally:
        context.close()


def test_late_response_cannot_replace_new_selection(disposable_browser, sidebar_server_url):
    context = disposable_browser.new_context(viewport={"width": 1138, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    held = []
    delay_second = [False]

    def payload(key):
        agent = {**base["agent"], "session_id": key, "run_id": key, "run_revision": 100,
            "prompt": key, "response": key, "response_html": f"<p>{key}</p>", "history": []}
        return {**base, "agent": agent, "active_count": 0, "sessions": [
            {"session_id": item, "session_title": item, "running": False, "phase": "finished"}
            for item in ("primary", "second")]}

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        if key == "second" and delay_second[0]:
            held.append(route)
        else:
            route.fulfill(json=payload(key))

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions(),
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        expect(page.locator("#agent_response_question")).to_have_text("primary")
        delay_second[0] = True
        page.locator("[data-execution-session-id=second]").click()
        page.locator("[data-execution-session-id=primary]").click()
        expect(page.locator("#agent_response_question")).to_have_text("primary")
        assert held
        for route in held:
            route.fulfill(json=payload("second"))
        expect(page.locator("#agent_response_question")).to_have_text("primary")
        delay_second[0] = False
        page.locator("[data-execution-session-id=second]").click()
        expect(page.locator("#agent_response_question")).to_have_text("second")
        page.reload()
        expect(page.locator("#agent_response_question")).to_have_text("second")
        expect(page.locator("[data-execution-session-id=second]")).to_have_attribute("aria-pressed", "true")
    finally:
        context.close()


@pytest.mark.parametrize("decorated_user", [False, True])
def test_identical_chatgpt_response_after_virtualized_turn_replacement(disposable_browser, monkeypatch, decorated_user):
    from app.core import computer_use_agent as agent

    context = disposable_browser.new_context()
    page = context.new_page()
    message = "Continue the unfinished Agent task."

    def turns(user_id, assistant_id, prompt):
        if decorated_user:
            prompt = (
                '<div role="group" aria-label="context.md"><button>context.md</button></div>'
                f'<div data-testid="collapsible-user-message-content">{prompt}</div>'
                '<button data-testid="collapsible-user-message-toggle">Show more</button>'
            )
        return (
            f'<div data-message-author-role="user" data-message-id="{user_id}">{prompt}</div>'
            f'<div data-message-author-role="assistant" data-message-id="{assistant_id}">'
            '<pre><code>{"action":"bodycheck"}</code></pre></div>'
            '<div id="prompt-textarea" contenteditable="true"></div>'
        )

    page.set_content(turns("old-user", "old-assistant", "Previous observation"))
    monkeypatch.setattr(agent, "_submit_chromium_prompt", lambda *_args, **_kwargs:
        page.set_content(turns("new-user", "new-assistant", message)))
    monkeypatch.setattr(agent, "WEB_RESPONSE_MINIMUM_SECONDS", 0)
    monkeypatch.setattr(agent, "WEB_RESPONSE_STABLE_SECONDS", 0)
    try:
        response = agent._submit_and_wait(page, "chromium", message, lambda: False, timeout_seconds=1)
        assert agent.parse_agent_action(response) == {"action": "bodycheck"}
    finally:
        context.close()


def test_session_catalog_titles_and_global_capacity(disposable_browser, sidebar_server_url):
    context = disposable_browser.new_context(viewport={"width": 1161, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    url = "https://chatgpt.com/c/provider-title"
    catalog = fixtures._chatgpt_catalog_sessions({"id": "provider-title", "url": url,
        "title": "Official conversation title", "updated_at": "2026-09-06T00:00:00Z"})
    page.route("**/api/agent/status", lambda route: route.fulfill(json={**base,
        "agent": {**base["agent"], "session_id": "primary", "conversation_url": url},
        "sessions": [{"session_id": "primary", "session_title": "Temporary label",
            "conversation_url": url, "running": False, "phase": "finished"}],
        "active_count": 1, "concurrency_limit": 2}))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": catalog}))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        expect(page.locator("[data-agent-session-capacity]")).to_have_text("· 0 here · 1 of 2 overall")
        expect(page.locator(".agent-execution-session-title")).to_have_text("Official conversation title")
    finally:
        context.close()


def test_status_disconnect_recovers_selected_run_without_mutation(disposable_browser, sidebar_server_url):
    context = disposable_browser.new_context(viewport={"width": 1161, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    disconnected = [False]
    mutations = []
    requested = []
    page.add_init_script("sessionStorage.setItem('cachelikes:agent-execution-session:edge:chatgpt', 'second')")

    def status(route):
        requested.append(route.request.headers.get("x-cachelikes-agent-session"))
        if disconnected[0]:
            route.abort("internetdisconnected")
            return
        route.fulfill(json={**base, "agent": {**base["agent"], "session_id": "second",
            "run_id": "ongoing", "run_revision": 999, "running": True, "phase": "running",
            "prompt": "Ongoing task", "message": "Provider is generating", "history": []},
            "sessions": [{"session_id": "second", "session_title": "Ongoing task", "running": True}],
            "active_count": 1})

    def mutation(route):
        mutations.append(route.request.url)
        route.abort()

    page.route("**/api/agent/status", status)
    page.route("**/api/agent/stop", mutation)
    page.route("**/api/agent/ask", mutation)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions()}))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        expect(page.locator("#agent_response_question")).to_have_text("Ongoing task")
        disconnected[0] = True
        expect(page.locator("#agent_response_status")).to_contain_text("Reconnecting", timeout=10000)
        expect(page.locator("[data-execution-session-id=second]")).to_have_attribute("aria-pressed", "true")
        disconnected[0] = False
        expect(page.locator("#agent_response_status")).to_contain_text("Provider is generating", timeout=10000)
        page.reload()
        expect(page.locator("#agent_response_question")).to_have_text("Ongoing task")
        assert requested and set(requested) == {"second"}
        assert mutations == []
    finally:
        context.close()


@pytest.mark.parametrize("width", [1161, 390])
def test_cross_project_sessions_switch_workspace_and_restore_on_reload(disposable_browser, sidebar_server_url, width):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    workspaces = {"primary": "/tmp/project-alpha", "second": "/tmp/project-beta"}
    catalog = [{"session_id": key, "session_title": key, "workspace_path": path,
                "running": True, "phase": "running"} for key, path in workspaces.items()]
    requests = []

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        path = route.request.headers.get("x-cachelikes-agent-workspace", "")
        requests.append((key, path))
        if key == "new":
            route.fulfill(json={**base, "agent": {"session_id": "new"}, "sessions": catalog,
                                "active_count": 2, "can_start": False})
            return
        matched = path == workspaces[key]
        route.fulfill(json={**base, "sessions": catalog, "active_count": 2,
            "agent": {**base["agent"], "session_id": key, "workspace_path": workspaces[key],
                "running": True, "phase": "running", "run_id": key, "run_revision": 999,
                "prompt": f"Task {key}" if matched else "", "history": []}})

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions()}))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        expect(page.locator(".agent-execution-session")).to_have_count(2)
        page.locator("[data-execution-session-id=second]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Task second")
        expect(page.locator("[data-agent-project-name]")).to_have_text("project-beta")
        page.reload()
        expect(page.locator("#agent_response_question")).to_have_text("Task second")
        expect(page.locator("[data-agent-project-name]")).to_have_text("project-beta")
        assert ("second", workspaces["second"]) in requests
        if width < 900 and not page.locator("[data-execution-session-id=primary]").is_visible():
            page.locator("#sidebar_toggle").click()
        page.locator("[data-execution-session-id=primary]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Task primary")
        expect(page.locator("[data-agent-project-name]")).to_have_text("project-alpha")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1161, 390])
def test_expired_execution_session_recovers_without_mutation(disposable_browser, sidebar_server_url, width):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    requested, mutations = [], []
    page.add_init_script("sessionStorage.setItem('cachelikes:agent-execution-session:edge:chatgpt', 'expired')")

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session")
        requested.append(key)
        if key == "expired":
            route.fulfill(status=404, json={"code": "unknown_agent_session", "error": "Session unavailable"})
        else:
            route.fulfill(json={**base, "agent": {"session_id": "new"}, "sessions": [],
                                "active_count": 0, "can_start": True})

    page.route("**/api/agent/status", status)
    page.route("**/api/agent/ask", lambda route: (mutations.append(route.request.url), route.abort()))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions()}))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        expect(page.get_by_role("button", name="Ask ChatGPT Web", exact=True)).to_be_enabled()
        assert requested[:2] == ["expired", "new"]
        assert page.evaluate("sessionStorage.getItem('cachelikes:agent-execution-session:edge:chatgpt')") == "new"
        assert mutations == []
    finally:
        context.close()


@pytest.mark.parametrize("choice,target,label,scope", [
    ("platform", "grok", "Grok", "edge:grok"),
    ("browser", "chrome", "ChatGPT", "chrome:chatgpt"),
])
def test_route_switch_isolates_worker_and_obeys_global_admission(disposable_browser, sidebar_server_url, choice, target, label, scope):
    context = disposable_browser.new_context(viewport={"width": 1161, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    requested = []
    page.add_init_script("sessionStorage.setItem('cachelikes:agent-execution-session:edge:chatgpt', 'original')")

    def status(route):
        headers = route.request.headers
        platform = headers["x-cachelikes-agent-platform"]
        key = headers["x-cachelikes-agent-session"]
        browser = headers["x-cachelikes-agent-browser"]
        current_scope = f"{browser}:{platform}"
        requested.append((current_scope, key))
        route.fulfill(json={**base, "agent": {"session_id": key},
            "sessions": [{"session_id": "original", "running": True}] if current_scope == "edge:chatgpt" else [],
            "active_count": 1, "can_start": current_scope == "edge:chatgpt"})

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions()}))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        expect(page.get_by_role("button", name="Ask ChatGPT Web", exact=True)).to_be_enabled()
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-trigger]').click()
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-option="{target}"]').click()
        expect(page.get_by_role("button", name=f"Ask {label} Web", exact=True)).to_be_disabled()
        page.wait_for_function("scope => sessionStorage.getItem('cachelikes:agent-execution-session:' + scope) === 'new'", arg=scope)
        assert (scope, "new") in requested
        assert (scope, "original") not in requested
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-trigger]').click()
        original = "chatgpt" if choice == "platform" else "edge"
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-option="{original}"]').click()
        expect(page.get_by_role("button", name="Ask ChatGPT Web", exact=True)).to_be_enabled()
        assert requested[-1] == ("edge:chatgpt", "original")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1138, 390])
def test_restored_session_loads_bound_history_and_ignores_late_reply(
    disposable_browser, sidebar_server_url, width
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    held = []
    requested = []
    sessions = [
        {"session_id": key, "session_title": key, "conversation_url": f"https://chatgpt.com/c/{key}",
         "running": False, "phase": "finished"}
        for key in ("primary", "restored", "late", "unavailable")
    ]

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "new")
        agent = {**base["agent"], "session_id": key, "run_id": key, "run_revision": 100,
                 "conversation_url": f"https://chatgpt.com/c/{key}", "conversation_bound": True,
                 "history": [], "prompt": "", "response": "", "response_html": ""}
        route.fulfill(json={**base, "agent": agent, "sessions": sessions, "active_count": 0})

    def history(route):
        from urllib.parse import parse_qs, urlsplit

        key = parse_qs(urlsplit(route.request.url).query)["conversation_url"][0].rsplit("/", 1)[-1]
        requested.append(key)
        if key == "late":
            held.append(route)
        elif key == "unavailable":
            route.fulfill(status=503, json={"error": "History service unavailable"})
        else:
            route.fulfill(json={"history": [{"prompt": f"Question {key}", "response": f"Answer {key}",
                                            "response_html": f"<p>Answer {key}</p>"}]})

    page.route("**/api/agent/status", status)
    page.route("**/api/agent/chatgpt-session-history?*", history)

    def default_worker_document(route):
        import re

        response = route.fetch()
        body = re.sub(r'data-agent-run-id="[^"]*"', 'data-agent-run-id="primary"', response.text())
        body = re.sub(r'data-agent-run-revision="[^"]*"', 'data-agent-run-revision="700"', body)
        route.fulfill(response=response, body=body)

    page.route("**/agent/edge/chatgpt", default_worker_document)
    catalog = fixtures._chatgpt_catalog_sessions()
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": catalog,
    }))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        expect(page.locator("#agent_response_question")).to_have_text("Question primary")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        page.locator("[data-execution-session-id=restored]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Question restored")
        expect(page.locator("#agent_response_answer")).to_contain_text("Answer restored")
        page.locator("[data-execution-session-id=unavailable]").click()
        expect(page.locator("#agent_response_status")).to_contain_text("History unavailable")
        page.locator("[data-execution-session-id=late]").click()
        expect(page.locator("#agent_response_status")).to_contain_text("Loading")
        page.locator("[data-execution-session-id=restored]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Question restored")
        assert held
        held.pop().fulfill(json={"history": [{"prompt": "Wrong question", "response": "Wrong answer",
                                            "response_html": "<p>Wrong answer</p>"}]})
        expect(page.locator("#agent_response_question")).to_have_text("Question restored")
        page.reload()
        expect(page.locator("#agent_response_question")).to_have_text("Question restored")
        assert requested.count("late") == 1
        assert requested.count("unavailable") == 1
        assert requested.count("restored") == 3
    finally:
        context.close()


@pytest.mark.parametrize("choice,target", [("platform", "grok"), ("browser", "chrome")])
@pytest.mark.parametrize("width", [1161, 390])
def test_new_session_drafts_are_isolated_by_route(
    disposable_browser, sidebar_server_url, choice, target, width
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    catalog = fixtures._chatgpt_catalog_sessions()
    page.route("**/api/agent/status", lambda route: route.fulfill(json={
        **base, "agent": {"session_id": "new"}, "sessions": [], "active_count": 0, "can_start": True,
    }))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": catalog,
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        prompt = page.locator("#agent_prompt_input")
        prompt.fill("Draft for Edge ChatGPT")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        trigger = page.locator(f'.agent-{choice}-combobox [data-agent-combobox-trigger]')
        trigger.click()
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-option="{target}"]').click()
        expect(prompt).to_have_value("")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        prompt.fill("Draft for the other route")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        trigger.click()
        original = "chatgpt" if choice == "platform" else "edge"
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-option="{original}"]').click()
        expect(prompt).to_have_value("Draft for Edge ChatGPT")
        trigger.click()
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-option="{target}"]').click()
        expect(prompt).to_have_value("Draft for the other route")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1138, 390])
def test_execution_session_restores_workspace_and_project(disposable_browser, sidebar_server_url, width):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    agents = {}
    for key in ("primary", "second"):
        agents[key] = {**base["agent"], "session_id": key, "run_id": key,
            "browser": "edge", "platform": "chatgpt", "workspace_path": f"/tmp/{key}",
            "session_mode": "project_session", "project_url": f"https://chatgpt.com/g/g-p-{key}/project",
            "conversation_url": f"https://chatgpt.com/c/{key}", "conversation_bound": True,
            "session_title": f"Task {key}", "running": False, "phase": "failed"}

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        route.fulfill(json={**base, "agent": agents.get(key, {"session_id": key}),
            "sessions": list(agents.values()), "active_count": 0, "can_start": True})

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": {"recent_sessions": [], "projects": []}}))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        for key in ("second", "primary", "second"):
            page.locator(f"[data-execution-session-id={key}]").click()
            expect(page.locator('input[name="workspace_path"]')).to_have_value(f"/tmp/{key}")
            expect(page.locator('input[name="project_url"]')).to_have_value(agents[key]["project_url"])
            expect(page.locator('input[name="conversation_url"]')).to_have_value(agents[key]["conversation_url"])
            expect(page.locator('input[name="session_mode"]')).to_have_value("project_session")
        page.reload()
        expect(page.locator('input[name="workspace_path"]')).to_have_value("/tmp/second")
        expect(page.locator('input[name="project_url"]')).to_have_value(agents["second"]["project_url"])
    finally:
        context.close()
