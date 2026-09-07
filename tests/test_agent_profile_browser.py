"""Profile-bound readiness and stale-response browser regressions.

Code version: v1.2.1-codex.1
"""

import json

import pytest
from playwright.sync_api import expect

from tests import test_sidebar_e2e as fixtures
from tests.agent_browser_fixtures import browser_profile_identity

disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url


def ready_payload(identity, title="Profile session"):
    return {
        "profile_identity": identity, "can_download": True, "logged_in": True,
        "account_name": title, "browser": "edge", "platform": "chatgpt",
        "model_catalog_complete": True, "model_options": [{"key": "latest_available", "label": "Latest"}],
        "effort_catalog_complete": True, "available_efforts": ["High"],
        "agent_sources": {"recent_sessions": [{"id": title, "title": title,
            "url": f"https://chatgpt.com/c/{identity}"}], "projects": []},
    }


def initialize_status(page, server_url, identity, cached=None):
    page.goto(f"{server_url}/settings/style-tokens")
    page.set_content("""<div id="status" data-browser-session-platform="chatgpt"
        data-browser-session-scope="agent"><div data-role="browser-session-status">
        <span data-role="browser-session-account"></span>
        <span data-role="browser-session-checkmark"></span></div></div>""")
    page.evaluate("""({identity, cached}) => {
        document.querySelector('#status').dataset.browserProfileIdentities = JSON.stringify({edge: identity});
        window.statusEvents = [];
        if (cached) sessionStorage.setItem(cached.key, JSON.stringify({cached_at: Date.now(), payload: cached.payload}));
    }""", {"identity": identity, "cached": cached})
    page.add_script_tag(url=f"{server_url}/static/browser-session-status.js")
    page.evaluate("""() => { window.statusController = CACHELIKES_BROWSER_SESSION_STATUS.init(
        document.querySelector('#status'), {browserId: 'edge', onStateChange(payload, browser, state) {
            window.statusEvents.push({payload, browser, state});
        }}); }""")


@pytest.mark.parametrize("cache_key_identity,payload_identity", [("profile-a", "profile-a"), ("profile-b", "profile-a"), ("profile-b", None)])
def test_another_profile_cache_cannot_skip_readiness(disposable_browser, sidebar_server_url, cache_key_identity, payload_identity):
    context = disposable_browser.new_context()
    page = context.new_page()
    held = []
    page.route("**/api/browser-session?*", lambda route: held.append(route))
    try:
        initialize_status(page, sidebar_server_url, "profile-b", {
            "key": f"cachelikes:browser-session:v8:agent:chatgpt:edge:{cache_key_identity}",
            "payload": ready_payload(payload_identity, "Old account"),
        })
        expect(page.locator('#status')).to_have_class("is-browser-status-loading")
        page.wait_for_function("statusEvents.length > 0")
        assert held
        assert not page.evaluate("statusEvents.some(event => event.payload?.can_download)")
        held.pop().fulfill(json=ready_payload("profile-b", "New account"))
        expect(page.locator('[data-role=browser-session-account]')).to_have_text("New account")
    finally:
        context.close()


@pytest.mark.parametrize("identity", [None, "profile-a"])
def test_unbound_server_readiness_is_rejected(disposable_browser, sidebar_server_url, identity):
    context = disposable_browser.new_context()
    page = context.new_page()
    page.route("**/api/browser-session?*", lambda route: route.fulfill(json=ready_payload(identity)))
    try:
        initialize_status(page, sidebar_server_url, "profile-b")
        expect(page.locator('[data-role=browser-session-checkmark]')).to_have_attribute("data-status-state", "error")
        assert not page.evaluate("statusEvents.some(event => event.payload?.can_download)")
        assert page.evaluate("sessionStorage.getItem('cachelikes:browser-session:v8:agent:chatgpt:edge:profile-b')") is None
    finally:
        context.close()


def test_profile_change_does_not_coalesce_or_accept_old_flight(disposable_browser, sidebar_server_url):
    context = disposable_browser.new_context()
    page = context.new_page()
    held = []
    page.route("**/api/browser-session?*", lambda route: held.append(route))
    try:
        initialize_status(page, sidebar_server_url, "profile-a")
        page.wait_for_function("statusEvents.length > 0")
        assert len(held) == 1
        page.evaluate("statusController.setProfileIdentities({edge: 'profile-b'})")
        page.wait_for_function("statusEvents.filter(event => event.state === 'loading').length === 2")
        # A different profile starts its own flight even while the first request is pending.
        assert len(held) == 2
        held[1].fulfill(json=ready_payload("profile-b", "New account"))
        expect(page.locator('[data-role=browser-session-account]')).to_have_text("New account")
        with page.expect_response(lambda response: response.request == held[0].request):
            held[0].fulfill(json=ready_payload("profile-a", "Old account"))
        expect(page.locator('[data-role=browser-session-account]')).to_have_text("New account")
        assert not page.evaluate("statusEvents.some(event => event.payload?.profile_identity === 'profile-a')")
    finally:
        context.close()


@pytest.mark.parametrize("running_after_change", [False, True])
def test_runtime_profile_change_clears_sources_and_disables_ask_until_rechecked(disposable_browser, sidebar_server_url, running_after_change):
    context = disposable_browser.new_context(viewport={"width": 1138, "height": 959})
    page = context.new_page()
    profile_a = browser_profile_identity()
    profile_b = "profile-b"
    active = [profile_a]
    held = []
    payload = fixtures._finished_chatgpt_agent_payload()
    payload["agent"] = {"session_id": "new"}

    def status(route):
        runtime = {**payload["runtime"], "browser_profile_identities": {"edge": active[0]}}
        route.fulfill(json={**payload, "runtime": runtime, "sessions": [], "can_start": True})

    def readiness(route):
        if active[0] == profile_a:
            route.fulfill(json=ready_payload(profile_a, "Old profile session"))
        else:
            held.append(route)

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session?*", readiness)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        ask = page.get_by_role('button', name='Ask ChatGPT Web', exact=True)
        expect(ask).to_be_enabled()
        expect(page.locator('[data-recent-conversation-url]')).to_contain_text("Old profile session")
        if running_after_change:
            payload["agent"] = {
                **fixtures._finished_chatgpt_agent_payload()["agent"],
                "session_id": "new", "running": True, "phase": "running",
                "run_id": "owned-task", "run_revision": 1,
            }
        active[0] = profile_b
        if running_after_change:
            expect(page.get_by_role('button', name='Stop Agent task', exact=True)).to_be_enabled(timeout=10000)
        else:
            expect(ask).to_be_disabled(timeout=10000)
        expect(page.locator('[data-recent-conversation-url]')).to_have_count(0)
        assert held
        held.pop().fulfill(json=ready_payload(profile_b, "New profile session"))
        expect(page.locator('[data-role=browser-session-checkmark]')).to_have_attribute("data-status-state", "ready")
        expect(page.locator('[data-role=browser-session-account]')).to_have_text("ChatGPT account")
        if running_after_change:
            expect(page.get_by_role('button', name='Stop Agent task', exact=True)).to_be_enabled()
        else:
            expect(ask).to_be_enabled()
            expect(page.locator('[data-recent-conversation-url]')).to_contain_text("New profile session")
        keys = page.evaluate("Object.keys(sessionStorage).filter(key => key.startsWith('cachelikes:browser-session:'))")
        assert any(key.endswith(f":{profile_a}") for key in keys)
        assert any(key.endswith(f":{profile_b}") for key in keys)
        cached = page.evaluate("key => JSON.parse(sessionStorage.getItem(key))", f"cachelikes:browser-session:v8:agent:chatgpt:edge:{profile_b}")
        assert cached["payload"]["profile_identity"] == profile_b
        assert cached["payload"]["account_name"] == "New profile session"
        assert json.loads(page.locator('[data-agent-browser-session]').get_attribute('data-browser-profile-identities')) == {"edge": profile_b}
    finally:
        context.close()


def test_late_source_catalog_cannot_repopulate_the_previous_profile(disposable_browser, sidebar_server_url):
    context = disposable_browser.new_context(viewport={"width": 1138, "height": 959})
    page = context.new_page()
    profile_a = browser_profile_identity()
    active = [profile_a]
    held = []
    payload = fixtures._finished_chatgpt_agent_payload()
    payload["agent"] = {"session_id": "new"}

    def status(route):
        route.fulfill(json={**payload, "runtime": {**payload["runtime"],
            "browser_profile_identities": {"edge": active[0]}}, "sessions": [], "can_start": True})

    def readiness(route):
        ready = ready_payload(active[0], "New profile session")
        if active[0] == profile_a:
            ready.pop("agent_sources")
        route.fulfill(json=ready)

    def hold_source(route):
        held.append(route)
        page.evaluate("window.__testSourceRouteHeld = true")

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session?*", readiness)
    page.route("**/api/agent/sources?*", hold_source)
    try:
        with page.expect_request("**/api/agent/sources?*"):
            page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        # Request emission precedes route dispatch. Change profiles only after
        # the test owns the pending route that it will release below.
        page.wait_for_function("window.__testSourceRouteHeld === true")
        assert held
        active[0] = "profile-b"
        expect(page.locator('[data-recent-conversation-url]')).to_contain_text("New profile session", timeout=10000)
        held.pop().fulfill(json={"profile_identity": profile_a, "projects": [],
            "recent_sessions": [{"url": "https://chatgpt.com/c/old", "title": "Stale private session"}]})
        expect(page.locator('[data-recent-conversation-url]')).to_contain_text("New profile session")
        expect(page.locator('[data-recent-conversation-url]')).to_have_count(1)
    finally:
        context.close()


def test_ask_carries_verified_profile_and_rejects_a_settings_change_before_poll(disposable_browser, sidebar_server_url):
    context = disposable_browser.new_context(viewport={"width": 1138, "height": 959})
    page = context.new_page()
    profile_a = browser_profile_identity()
    server_profile = [profile_a]
    submitted = []
    started = []
    held_status = []
    release_updated_status = [False]
    base = fixtures._finished_chatgpt_agent_payload()
    base["agent"] = {"session_id": "new"}
    base["runtime"]["browser_profile_identities"] = {"edge": profile_a}

    def status(route):
        if server_profile[0] == profile_a:
            route.fulfill(json={**base, "sessions": [], "can_start": True})
        elif release_updated_status[0]:
            route.fulfill(json={**base, "runtime": {**base["runtime"],
                "browser_profile_identities": {"edge": server_profile[0]}}, "sessions": [], "can_start": True})
        else:
            # Keep the settings change unknown to the open Agent page until after submission.
            held_status.append(route)

    def ask(route):
        submission = route.request.post_data_json
        submitted.append(submission)
        if submission.get("profile_identity") != server_profile[0]:
            route.fulfill(status=409, json={"code": "browser_profile_changed",
                "error": "Browser profile settings changed. Recheck before starting a task."})
        else:
            started.append(submission)
            route.fulfill(json={**base, "agent": {"session_id": "started", "running": True}})

    page.route("**/api/agent/status", status)
    page.route("**/api/agent/preferences", lambda route: route.fulfill(json=base))
    page.route("**/api/browser-session?*", lambda route: route.fulfill(json=ready_payload(server_profile[0])))
    page.route("**/api/agent/ask", ask)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        button = page.get_by_role("button", name="Ask ChatGPT Web", exact=True)
        expect(button).to_be_enabled()
        page.get_by_placeholder("Do anything", exact=True).fill("Keep this draft in the verified profile.")
        # The other settings page saves profile B while this page still has A's readiness.
        server_profile[0] = "profile-b"
        with page.expect_response(lambda response: response.url.endswith("/api/agent/ask") and response.status == 409):
            button.click()
        assert len(submitted) == 1
        assert submitted[0]["profile_identity"] == profile_a
        assert started == []
        expect(page.locator('#agent_response_status')).to_have_attribute("data-status", "failed")
        expect(page.locator('#agent_error_record')).to_contain_text("Browser profile settings changed")
        expect(button).to_be_disabled()
        expect(page.get_by_role("button", name="Stop Agent task", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Recheck", exact=True)).to_be_visible()
        expect(page.get_by_placeholder("Do anything", exact=True)).to_have_value("Keep this draft in the verified profile.")
        release_updated_status[0] = True
        for route in held_status:
            status(route)
        page.wait_for_function("JSON.parse(document.querySelector('[data-agent-browser-session]').dataset.browserProfileIdentities).edge === 'profile-b'")
        expect(button).to_be_enabled()
        expect(page.get_by_placeholder("Do anything", exact=True)).to_have_value("Keep this draft in the verified profile.")
        assert len(submitted) == 1
        assert started == []
    finally:
        context.close()
