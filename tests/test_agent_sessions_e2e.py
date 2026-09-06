"""Session switching, capacity, and selected controls. Version: v1.0.2-codex.1."""

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
    disconnected = [False]
    mutations = []
    requested = []
    page.add_init_script("sessionStorage.setItem('cachelikes:agent-execution-session', 'second')")

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
    workspaces = {"primary": "/tmp/project-alpha", "second": "/tmp/project-beta"}
    catalog = [{"session_id": key, "session_title": key, "workspace_path": path,
                "running": True, "phase": "running"} for key, path in workspaces.items()]
    requests = []

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        path = route.request.headers.get("x-cachelikes-agent-workspace", "")
        requests.append((key, path))
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
