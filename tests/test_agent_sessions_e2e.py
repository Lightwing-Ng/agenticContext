"""Session switching, capacity, and selected controls. Code version: v1.21.0-codex.0."""

import re
from copy import deepcopy
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from tests import test_sidebar_e2e as fixtures
from app.core.browser_sessions import grok_composer_snapshot
from app.web.presentation import render_agent_response

disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url


@pytest.fixture()
def agent_selection_server_url(tmp_path):
    """Serve one isolated Agent settings store for persistence interaction tests."""
    from app.web.app import create_app

    application = create_app(
        tmp_path / "local-store",
        computer_use_settings_path=tmp_path / "settings" / "computer-use-agent.json",
        computer_use_runtime_root=tmp_path / "computer-use-runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.parametrize("width", [1008, 390])
def test_agent_connection_mode_switch_keeps_recent_sessions(
    disposable_browser,
    sidebar_server_url,
    width,
):
    """Tunnel hides Browser setup while preserving the shared recent-session rail."""
    context = disposable_browser.new_context(viewport={"width": width, "height": 820})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/agent/status", lambda route: route.fulfill(
        json=fixtures._finished_chatgpt_agent_payload(),
    ))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "logged_in": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions(),
    }))
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()

        mode_control = page.locator("[data-agent-connection-mode-control]")
        browser = page.locator("#agent_connection_browser")
        tunnel = page.locator("#agent_connection_tunnel")
        browser_fields = page.locator("[data-agent-browser-mode-field]")
        recent_sessions = page.locator("[data-agent-execution-sessions]")
        workspace = page.locator("#agent_workspace")

        expect(mode_control).to_be_visible()
        material = page.evaluate(
            """() => {
                const read = (element) => {
                    const style = getComputedStyle(element);
                    return {
                        backgroundColor: style.backgroundColor,
                        borderRadius: style.borderRadius,
                        minHeight: style.minHeight,
                        boxShadow: style.boxShadow,
                    };
                };
                return {
                    connection: read(document.querySelector('[data-agent-connection-mode-control]')),
                    agentMode: read(document.querySelector('.agent-mode-control')),
                };
            }"""
        )
        assert material["connection"] == material["agentMode"]
        web_service = page.locator(".agent-platform-combobox")
        tunnel_field = page.locator("[data-agent-tunnel-mode-field]")
        onboarding = page.locator('[data-agent-tunnel-provider-panel="chatgpt"]')
        browser_task = page.locator("[data-agent-browser-task]")
        expect(browser).to_be_checked()
        expect(tunnel).not_to_be_checked()
        # Web service is shared by both connections, so the switch sits right after it.
        assert page.evaluate(
            """() => {
                const platform = document.querySelector('.agent-platform-combobox').closest('label.field');
                return platform.nextElementSibling === document.querySelector('[data-agent-connection-mode-control]');
            }"""
        )
        expect(browser_fields).to_have_count(3)
        for field in browser_fields.all():
            expect(field).to_be_visible()
        expect(recent_sessions).to_be_visible()
        expect(tunnel_field).to_be_hidden()
        expect(browser_task).to_be_visible()
        expect(onboarding).to_be_hidden()
        assert workspace.get_attribute("data-layout-role") is None

        page.locator('label[for="agent_connection_tunnel"]').click()
        expect(tunnel).to_be_checked()
        expect(page).to_have_url(re.compile(r"/agent/tunnel/chatgpt$"))
        expect(web_service).to_be_visible()
        for field in browser_fields.all():
            expect(field).to_be_hidden()
        expect(recent_sessions).to_be_hidden()
        expect(tunnel_field).to_be_visible()
        expect(browser_task).to_be_hidden()
        expect(page.locator("#agent_prompt_form")).to_be_hidden()
        expect(onboarding).to_be_visible()
        expect(workspace).to_have_attribute("data-layout-role", "content-scrollport")
        expect(page.locator("[data-agent-heading]")).to_have_text("Connect ChatGPT to this local project")
        assert onboarding.locator(".agent-tunnel-step-number").all_inner_texts() == [
            "Step 1",
            "Step 2",
            "Step 3",
            "Step 4",
        ]
        assert onboarding.locator(".agent-tunnel-guide-number").all_inner_texts() == [
            "➊",
            "➋",
            "➌",
            "➍",
            "➊",
            "➋",
        ]
        guides = onboarding.locator("details.ui-collapse[data-agent-tunnel-guide]")
        expect(guides).to_have_count(6)
        assert guides.evaluate_all("nodes => nodes.map((node) => node.open)") == [False] * 6
        external_actions = onboarding.locator("a.agent-tunnel-step-action")
        assert external_actions.count() == 5
        for action in external_actions.all():
            expect(action).to_have_class(re.compile(r"\bsecondary-button\b"))
            expect(action).to_have_attribute("target", "_blank")
            expect(action).to_have_attribute("rel", "noopener noreferrer")
        visible_external_actions = onboarding.locator("a.agent-tunnel-step-action:visible")
        expect(visible_external_actions).to_have_count(4)
        expect(onboarding.get_by_role("link", name="Ask in ChatGPT")).to_be_hidden()
        right_edges = visible_external_actions.evaluate_all(
            "(nodes) => nodes.map((node) => node.getBoundingClientRect().right)"
        )
        assert max(right_edges) - min(right_edges) <= 2
        expect(page.locator("[data-agent-tunnel-state]")).to_have_text(
            re.compile(r"^(Ready|Active|Connecting|Not configured|Not running|Disconnected|Unavailable)$")
        )

        page.locator('label[for="agent_connection_browser"]').click()
        expect(browser).to_be_checked()
        expect(page).to_have_url(re.compile(r"/agent/edge/chatgpt$"))
        for field in browser_fields.all():
            expect(field).to_be_visible()
        expect(recent_sessions).to_be_visible()
        expect(tunnel_field).to_be_hidden()
        expect(browser_task).to_be_visible()
        expect(onboarding).to_be_hidden()
        assert workspace.get_attribute("data-layout-role") is None
        expect(page.locator("[data-agent-heading]")).to_have_text("ChatGPT Web Agent")
        assert errors == []
    finally:
        context.close()



TUNNEL_ONBOARDING_ID = "tunnel_" + "b" * 32
TUNNEL_ONBOARDING_KEY = "sk-proj-onboarding-test-ABCD"


def _tunnel_onboarding_status(enabled=True, *, activity_observed=True):
    return {
        "enabled": enabled,
        "state": "ready" if enabled else "disconnected",
        "ready": enabled,
        "activity_observed": activity_observed,
        "credentials": {
            "tunnel_id": TUNNEL_ONBOARDING_ID,
            "tunnel_id_valid": True,
            "api_key_saved": True,
            "api_key_hint": "…ABCD",
            "qualified": True,
        },
        "presentation": {
            "tone": "ready" if enabled else "error",
            "label": "Tunnel ready" if enabled else "Disconnected",
            "message": "Ready for a project tool call." if enabled else "Disconnected.",
            "hint": "",
            "action": None,
        },
    }


def _empty_gemini_authorization():
    return {
        "pending": False,
        "review_id": "",
        "redirect_uri": "",
        "redirect_host": "",
        "expires_in": 0,
    }


def _gemini_tunnel_status(
    public_origin="https://agent.example.com",
    authorization=None,
):
    configured = bool(public_origin)
    return {
        "platform": "gemini",
        "activity_observed": False,
        "presentation": {
            "tone": "configured" if configured else "error",
            "label": "Configured" if configured else "Not configured",
            "message": (
                "Gemini connection values are configured."
                if configured
                else "Save a public HTTPS origin."
            ),
            "hint": "",
            "action": None,
        },
        "config": {
            "configured": configured,
            "public_origin": public_origin,
            "mcp_url": f"{public_origin}/mcp/gemini" if configured else "",
            "client_id": "gtc_frontend_test" if configured else "",
            "client_secret_saved": configured,
            "signing_key_saved": configured,
        },
        "authorization": (
            authorization
            if authorization is not None
            else _empty_gemini_authorization()
        ),
        "active_calls": [],
        "recent_calls": [],
    }


@pytest.mark.parametrize("width", [1280, 876, 390])
def test_tunnel_kickoff_copy_and_native_monospace(
    disposable_browser,
    sidebar_server_url,
    width,
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 900})
    context.add_init_script("""Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: {writeText: async (text) => { window.copiedKickoff = text; }}
    });""")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(
        "**/api/agent/tunnel/status?platform=chatgpt",
        lambda route: route.fulfill(json=_tunnel_onboarding_status()),
    )
    try:
        page.goto(sidebar_server_url + "/agent/tunnel/chatgpt")
        if width < 900 and "is-sidebar-collapsed" not in page.locator("#app_shell").get_attribute("class"):
            page.locator("#sidebar_toggle").click()
        onboarding = page.locator('[data-agent-tunnel-provider-panel="chatgpt"]')
        kickoff = onboarding.locator("[data-agent-tunnel-kickoff]")
        expect(kickoff).to_have_value(re.compile(r"@AgenticContext"))
        expect(kickoff).to_have_value(re.compile(r"\[describe your task\]"))
        assert kickoff.input_value().endswith(
            "without altering unrelated work:\n[describe your task]."
        )
        expect(onboarding).to_contain_text("named AgenticContext")
        expect(onboarding).to_contain_text("starting with tunnel_")
        expect(onboarding).to_contain_text("sk-proj-")
        expect(onboarding).not_to_contain_text("tunnel_*4766")
        expect(onboarding).not_to_contain_text("sk-proj-*HMAA")
        expect(onboarding.locator(".agent-tunnel-onboarding-step")).to_have_count(4)
        expect(onboarding.locator("details.ui-collapse[data-agent-tunnel-guide]")).to_have_count(6)
        expect(onboarding.locator("[data-agent-tunnel-guide] svg[role='img']")).to_have_count(6)
        expect(onboarding.locator("[data-agent-tunnel-guide] :is(img, image, foreignObject)")).to_have_count(0)
        expect(page.locator("#agent_prompt_form")).to_be_hidden()
        technical_input_styles = page.locator(
            "#agent_project_path, #chatgpt_tunnel_id, #chatgpt_tunnel_api_key"
        ).evaluate_all(
            """inputs => inputs.map((input) => ({
                id: input.id,
                fontFamily: getComputedStyle(input).fontFamily,
                fontSize: getComputedStyle(input).fontSize,
                placeholderFontFamily: getComputedStyle(input, '::placeholder').fontFamily,
                placeholderFontSize: getComputedStyle(input, '::placeholder').fontSize,
            }))"""
        )
        assert [item["fontFamily"] for item in technical_input_styles] == [
            "monospace",
            "monospace",
            "monospace",
        ]
        credential_styles = technical_input_styles[1:]
        assert len({item["fontSize"] for item in credential_styles}) == 1
        assert {
            item["placeholderFontFamily"] for item in credential_styles
        } == {"monospace"}
        assert {
            item["placeholderFontSize"] for item in credential_styles
        } == {credential_styles[0]["fontSize"]}
        button = onboarding.locator("[data-agent-tunnel-copy-kickoff]")
        next_step = onboarding.locator("[data-agent-tunnel-kickoff-next-step]")
        expect(button).to_have_text("Copy this prompt")
        expect(next_step).to_have_text("Edit and copy the prompt")
        expect(onboarding.locator("[data-agent-tunnel-kickoff-title]")).to_have_text(
            "Describe your project task"
        )
        kickoff.fill("Short task.")
        short_geometry = kickoff.evaluate(
            """element => ({
                height: element.getBoundingClientRect().height,
                overflowY: getComputedStyle(element).overflowY,
                paddingTop: getComputedStyle(element).paddingTop,
                paddingBottom: getComputedStyle(element).paddingBottom,
            })"""
        )
        assert 40 <= short_geometry["height"] < 96
        assert short_geometry["overflowY"] == "hidden"
        assert short_geometry["paddingTop"] == "8px"
        assert short_geometry["paddingBottom"] == "8px"
        kickoff.fill("\n".join(f"Task detail {index}" for index in range(10)))
        long_geometry = kickoff.evaluate(
            """element => ({
                height: element.getBoundingClientRect().height,
                overflowY: getComputedStyle(element).overflowY,
                scrollHeight: element.scrollHeight,
                clientHeight: element.clientHeight,
            })"""
        )
        assert long_geometry["height"] == 96
        assert long_geometry["overflowY"] == "auto"
        assert long_geometry["scrollHeight"] > long_geometry["clientHeight"]
        edited_prompt = "Use @AgenticContext to verify the responsive Tunnel guide."
        kickoff.fill(edited_prompt)
        button.scroll_into_view_if_needed()
        button.focus()
        button.press("Enter")
        expect(button).to_have_text("Copied")
        expect(next_step).to_have_text("Open ChatGPT and ask your question.")
        assert page.evaluate("window.copiedKickoff") == edited_prompt
        expect(button).to_be_focused()
        kickoff.fill(edited_prompt + " Check the narrow viewport.")
        expect(button).to_have_text("Copy this prompt")
        expect(next_step).to_have_text("Edit and copy the prompt")
        edges = onboarding.locator(
            "a.agent-tunnel-step-action, [data-agent-tunnel-copy-kickoff]"
        ).evaluate_all(
            "nodes => nodes.map(e => e.getBoundingClientRect().right)"
        )
        assert max(edges) - min(edges) <= 2
        expect(onboarding.locator("[data-agent-tunnel-toggle]")).to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if width == 1280:
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(100)
            assert kickoff.evaluate("element => element.clientHeight <= 96")
        assert not errors
    finally:
        context.close()


def test_tunnel_kickoff_unlocks_only_after_credentials_and_plugin_activity(
    disposable_browser,
    sidebar_server_url,
):
    """Keep the task editor behind the verified setup preconditions."""
    context = disposable_browser.new_context(viewport={"width": 876, "height": 1_100})
    page = context.new_page()
    status = _tunnel_onboarding_status(activity_observed=False)
    status["credentials"] = {
        "tunnel_id": "",
        "tunnel_id_valid": False,
        "api_key_saved": False,
        "api_key_hint": "",
        "qualified": False,
    }

    def fulfill_status(route):
        route.fulfill(json=deepcopy(status))

    def refresh_tunnel_status():
        for input_id in ("agent_connection_browser", "agent_connection_tunnel"):
            page.locator(f"#{input_id}").evaluate(
                """element => {
                    element.checked = true;
                    element.dispatchEvent(new Event('change', {bubbles: true}));
                }"""
            )

    page.route("**/api/agent/tunnel/status?platform=chatgpt", fulfill_status)
    try:
        page.goto(sidebar_server_url + "/agent/tunnel/chatgpt")
        title = page.locator("[data-agent-tunnel-kickoff-title]")
        kickoff = page.locator("[data-agent-tunnel-kickoff]")
        copy_form = page.locator("[data-agent-tunnel-kickoff-copy-form]")
        action_step = page.locator("[data-agent-tunnel-kickoff-action-step]")
        expect(title).to_have_text("Complete the Tunnel credentials")
        expect(kickoff).to_be_hidden()
        expect(kickoff).to_be_disabled()
        expect(copy_form).to_be_hidden()
        expect(action_step).to_be_hidden()

        status.update(_tunnel_onboarding_status(activity_observed=False))
        refresh_tunnel_status()
        expect(title).to_have_text("Create the AgenticContext plugin")
        expect(kickoff).to_be_hidden()
        expect(action_step).to_be_hidden()

        status["activity_observed"] = True
        refresh_tunnel_status()
        expect(title).to_have_text("Describe your project task")
        expect(kickoff).to_be_visible()
        expect(kickoff).to_be_enabled()
        expect(copy_form).to_be_visible()
        expect(action_step).to_be_visible()
        expect(action_step.locator("[data-agent-tunnel-kickoff-next-step]")).to_have_text(
            "Edit and copy the prompt"
        )
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_tunnel_guides_use_native_disclosure_and_vector_cards(
    disposable_browser,
    sidebar_server_url,
    width,
):
    """Keep all six visual guides native, independent, and closed by default."""
    context = disposable_browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(sidebar_server_url + "/agent/tunnel/chatgpt")
        if width < 900 and "is-sidebar-collapsed" not in page.locator("#app_shell").get_attribute("class"):
            page.locator("#sidebar_toggle").click()

        onboarding = page.locator('[data-agent-tunnel-provider-panel="chatgpt"]')
        guides = onboarding.locator("details.ui-collapse[data-agent-tunnel-guide]")
        summaries = guides.locator("summary")
        expect(guides).to_have_count(6)
        expect(summaries).to_have_count(6)
        assert onboarding.locator(".agent-tunnel-guide-title").all_inner_texts() == [
            "Create a tunnel",
            "Copy the Tunnel ID",
            "Create an API key",
            "Copy the secret key",
            "Enable Developer mode",
            "Create the AgenticContext plugin",
        ]
        expect(onboarding.locator(".agent-tunnel-guide-description")).to_have_count(0)
        expect(onboarding.locator("[data-agent-tunnel-guide] circle")).to_have_count(0)
        expect(onboarding.locator("[data-agent-tunnel-guide] .guide-card")).to_have_count(4)
        expect(onboarding.locator("[data-agent-tunnel-guide] [class*='guide-window']")).to_have_count(0)
        assert onboarding.locator("[data-agent-tunnel-guide] rect").evaluate_all(
            "nodes => nodes.every((node) => node.getAttribute('rx') === '10')"
        )
        expect(onboarding.locator('[data-guide-expiration="never"]')).to_have_count(1)
        expect(onboarding.locator('[data-guide-selected="all"]')).to_have_count(1)
        expect(onboarding.locator('[data-guide-selected="tunnel"]')).to_have_count(1)
        expect(onboarding.locator('[data-guide-auth="none"]')).to_have_count(1)
        expect(onboarding).not_to_contain_text("Read + Use")
        assert onboarding.locator("[data-agent-tunnel-guide] svg").evaluate_all(
            "nodes => nodes.every((node) => node.viewBox.baseVal.width === 640 && node.viewBox.baseVal.height <= 1024)"
        )
        assert guides.evaluate_all("nodes => nodes.map((node) => node.open)") == [False] * 6
        for svg in onboarding.locator("[data-agent-tunnel-guide] svg[role='img']").all():
            expect(svg).to_be_hidden()

        summaries.nth(0).focus()
        summaries.nth(0).press("Enter")
        expect(guides.nth(0)).to_have_attribute("open", "")
        expect(guides.nth(0).locator("svg")).to_be_visible()
        expect(summaries.nth(0)).to_be_focused()

        summaries.nth(1).focus()
        summaries.nth(1).press("Space")
        expect(guides.nth(1)).to_have_attribute("open", "")
        assert guides.evaluate_all("nodes => nodes.map((node) => node.open)")[:2] == [True, True]
        expect(summaries.nth(1)).to_be_focused()

        summaries.nth(0).click()
        expect(guides.nth(0)).not_to_have_attribute("open", "")
        expect(guides.nth(1)).to_have_attribute("open", "")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

        page.reload()
        assert guides.evaluate_all("nodes => nodes.map((node) => node.open)") == [False] * 6
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize(
    ("width", "height"),
    [(1280, 420), (390, 420), (1280, 900), (876, 1190)],
)
def test_tunnel_onboarding_uses_page_content_scroll_and_step_hierarchy(
    disposable_browser,
    sidebar_server_url,
    width,
    height,
):
    """Keep Tunnel onboarding on one page-level scroll owner with compact hierarchy."""
    context = disposable_browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(
        "**/api/agent/tunnel/status?platform=chatgpt",
        lambda route: route.fulfill(json=_tunnel_onboarding_status()),
    )
    try:
        page.goto(sidebar_server_url + "/agent/tunnel/chatgpt")
        if width < 900 and "is-sidebar-collapsed" not in page.locator("#app_shell").get_attribute("class"):
            page.locator("#sidebar_toggle").click()

        onboarding = page.locator('[data-agent-tunnel-provider-panel="chatgpt"]')
        steps = onboarding.locator(".agent-tunnel-onboarding-step")
        expect(onboarding).to_be_visible()
        expect(steps).to_have_count(4)
        expect(page.locator("[data-agent-tunnel-message]")).to_have_count(0)
        expect(page.locator("[data-agent-tunnel-activity]")).to_have_count(0)
        expect(page.locator("[data-agent-tunnel-hint]")).to_be_hidden()

        guides = onboarding.locator("details[data-agent-tunnel-guide]")
        expect(guides).to_have_count(6)
        guides.evaluate_all("nodes => nodes.forEach((node) => { node.open = true; })")
        assert guides.evaluate_all("nodes => nodes.map((node) => node.open)") == [True] * 6

        action = onboarding.locator("[data-agent-tunnel-copy-kickoff]")
        action.focus()
        action.evaluate("element => element.scrollIntoView({block: 'nearest', inline: 'nearest'})")
        page.wait_for_timeout(250)

        geometry = page.evaluate(
            """() => {
                const workspace = document.querySelector('#agent_workspace');
                const grid = document.querySelector('.agent-workspace-grid');
                const card = document.querySelector('[data-agent-tunnel-provider-panel="chatgpt"]');
                const steps = [...card.querySelectorAll('.agent-tunnel-onboarding-step')];
                const action = card.querySelector('[data-agent-tunnel-copy-kickoff]');

                const scrollable = (element) => {
                    const style = getComputedStyle(element);
                    return ['auto', 'scroll'].includes(style.overflowY)
                        && element.scrollHeight > element.clientHeight + 1;
                };
                const center = (element) => {
                    const box = element.getBoundingClientRect();
                    return box.top + box.height / 2;
                };
                const rootStyle = getComputedStyle(document.documentElement);
                const token = (name) => parseFloat(rootStyle.getPropertyValue(name));
                const workspaceStyle = getComputedStyle(workspace);
                const actionStyle = getComputedStyle(action);
                const workspaceRect = workspace.getBoundingClientRect();
                const clip = {
                    top: workspaceRect.top + workspace.clientTop,
                    left: workspaceRect.left + workspace.clientLeft,
                    bottom: workspaceRect.top + workspace.clientTop + workspace.clientHeight,
                    right: workspaceRect.left + workspace.clientLeft + workspace.clientWidth,
                };
                const actionRect = action.getBoundingClientRect();
                const headings = steps.map((step) => step.querySelector('h4'));
                const paragraphs = steps.flatMap((step) => [...step.querySelectorAll('.agent-tunnel-step-copy p')]);
                const actionPackages = [...card.querySelectorAll('.agent-tunnel-action-package')];
                const guideItems = [...card.querySelectorAll('.agent-tunnel-guide-item')];
                const guideDetails = guideItems.map((item) => item.querySelector('details'));
                const guideSummaries = guideDetails.map((detail) => detail.querySelector('summary'));
                const guideTitles = guideDetails.map((detail) => detail.querySelector('.agent-tunnel-guide-title'));
                const guideBodies = guideDetails.map((detail) => detail.querySelector(':scope > .ui-collapse-body'));
                const guideSvgs = guideDetails.map((detail) => detail.querySelector('svg'));
                const credentialCopy = steps[1].querySelector('.agent-tunnel-step-copy');
                const credentialFields = steps[1].querySelector('.agent-tunnel-credential-fields');
                const credentialInputs = [...steps[1].querySelectorAll('.text-input-control')];
                const stepFourLink = steps[3].querySelector('a.agent-tunnel-step-action');
                const stepFourButton = steps[3].querySelector('[data-agent-tunnel-copy-kickoff]');
                const kickoff = steps[3].querySelector('[data-agent-tunnel-kickoff]');
                const titleText = document.createRange();
                titleText.selectNodeContents(document.querySelector('[data-agent-heading]'));
                const quickActions = document.querySelector('.global-quick-actions').getBoundingClientRect();
                return {
                    titleOverlapsQuickActions: [...titleText.getClientRects()].some((line) =>
                        line.right > quickActions.left && line.left < quickActions.right
                        && line.top < quickActions.bottom && line.bottom > quickActions.top
                    ),
                    workspaceOverflowY: workspaceStyle.overflowY,
                    workspaceScrollPaddingBlockEnd: parseFloat(workspaceStyle.scrollPaddingBlockEnd),
                    gridOverflowY: getComputedStyle(grid).overflowY,
                    cardOverflowY: getComputedStyle(card).overflowY,
                    workspaceScrollable: scrollable(workspace),
                    gridScrollable: scrollable(grid),
                    cardScrollable: scrollable(card),
                    // The content column, its descendants, and its ancestors up to the
                    // document; the navigation sidebar is a separate scroll region.
                    scrollOwners: [
                        ...(function* ancestors(node) {
                            for (let current = node.parentElement; current; current = current.parentElement) yield current;
                        })(workspace),
                        workspace,
                        ...workspace.querySelectorAll('*'),
                    ]
                        .filter((element) => element.checkVisibility() && scrollable(element))
                        .map((element) => element.id || element.tagName),
                    markerHeadingCenterDeltas: steps.map((step, index) => Math.abs(
                        center(step.querySelector('.agent-tunnel-step-number')) - center(headings[index])
                    )),
                    markerFontSizes: [...new Set(steps.map((step) =>
                        parseFloat(getComputedStyle(step.querySelector('.agent-tunnel-step-number')).fontSize)
                    ))],
                    markerColors: steps.map((step) =>
                        getComputedStyle(step.querySelector('.agent-tunnel-step-number')).color
                    ),
                    markerTexts: steps.map((step) =>
                        step.querySelector('.agent-tunnel-step-number').textContent.trim()
                    ),
                    headingColors: headings.map((heading) => getComputedStyle(heading).color),
                    guideMarkerTexts: guideItems.map((item) =>
                        item.querySelector('.agent-tunnel-guide-number').textContent.trim()
                    ),
                    guideMarkerFontSizes: guideItems.map((item) =>
                        parseFloat(getComputedStyle(item.querySelector('.agent-tunnel-guide-number')).fontSize)
                    ),
                    guideTitleFontSizes: guideTitles.map((title) =>
                        parseFloat(getComputedStyle(title).fontSize)
                    ),
                    guideSummaryTexts: guideSummaries.map((summary) => summary.innerText.trim()),
                    guideDetailsOpen: guideDetails.map((detail) => detail.open),
                    guideSummaryDisplay: [...new Set(guideSummaries.map((summary) =>
                        getComputedStyle(summary).display
                    ))],
                    guideSummaryMarkerMasks: guideSummaries.map((summary) =>
                        getComputedStyle(summary, '::after').maskImage
                    ),
                    guideSummaryStyles: guideSummaries.map((summary) => {
                        const style = getComputedStyle(summary);
                        const icon = getComputedStyle(summary, '::after');
                        const body = getComputedStyle(summary.parentElement.querySelector(':scope > .ui-collapse-body'));
                        return {
                            columnGap: style.columnGap,
                            padding: [style.paddingTop, style.paddingRight, style.paddingBottom, style.paddingLeft],
                            iconSize: [icon.width, icon.height],
                            bodyPadding: [body.paddingTop, body.paddingRight, body.paddingBottom, body.paddingLeft],
                        };
                    }),
                    guideBodyStyles: guideBodies.map((body) => ({
                        overflowX: getComputedStyle(body).overflowX,
                        overflowY: getComputedStyle(body).overflowY,
                        tabIndex: body.tabIndex,
                        role: body.getAttribute('role'),
                        labelledBy: body.getAttribute('aria-labelledby'),
                        horizontallyScrollable: body.scrollWidth > body.clientWidth + 1,
                        clientWidth: body.clientWidth,
                    })),
                    guideSvgClientWidths: guideSvgs.map((svg) => svg.getBoundingClientRect().width),
                    guideSvgViewBoxes: guideSvgs.map((svg) => ({
                        width: svg.viewBox.baseVal.width,
                        height: svg.viewBox.baseVal.height,
                    })),
                    guideSvgStyles: guideSvgs.map((svg) => {
                        const style = getComputedStyle(svg);
                        return {borderRadius: style.borderRadius, overflow: style.overflow};
                    }),
                    tunnelListCopyIconSize: (() => {
                        const bounds = guideSvgs[1].querySelector('.guide-copy-icon').getBBox();
                        return {width: bounds.width, height: bounds.height};
                    })(),
                    tunnelListHeaderButtonHeight: guideSvgs[1]
                        .querySelector('.guide-button-secondary').getBBox().height,
                    guideScrollportsInsideStep: guideBodies.map((body) => {
                        const bodyRect = body.getBoundingClientRect();
                        const ownerRect = body.closest('.agent-tunnel-step-copy').getBoundingClientRect();
                        return bodyRect.left >= ownerRect.left - 1 && bodyRect.right <= ownerRect.right + 1;
                    }),
                    guideStepNonOverlapping:
                        steps[0].getBoundingClientRect().bottom <= steps[1].getBoundingClientRect().top,
                    actionPackageRightDeltas: actionPackages.flatMap((pack) =>
                        [...pack.querySelectorAll('.agent-tunnel-action-package-form')].map((form) => Math.abs(
                            pack.getBoundingClientRect().right - form.getBoundingClientRect().right
                        ))
                    ),
                    actionPackagesFollowCopy: actionPackages.map((pack) => {
                        const actionForms = [...pack.querySelectorAll('.agent-tunnel-action-package-form')];
                        return actionForms.every((actionForm) => {
                            const paragraphs = [...actionForm.parentElement.querySelectorAll(':scope > p')];
                            return paragraphs.length === 0 || actionForm.getBoundingClientRect().top
                                >= paragraphs.at(-1).getBoundingClientRect().bottom;
                        });
                    }),
                    actionLabels: [...card.querySelectorAll('.agent-tunnel-step-action')].map((element) =>
                        element.textContent.trim()
                    ),
                    actionClasses: [...card.querySelectorAll('.agent-tunnel-step-action')].map((element) =>
                        element.className
                    ),
                    headingFontSizes: [...new Set(headings.map((heading) => parseFloat(getComputedStyle(heading).fontSize)))],
                    bodyFontSizes: [...new Set(paragraphs.map((paragraph) => parseFloat(getComputedStyle(paragraph).fontSize)))],
                    credentialInputFontSizes: credentialInputs.map((input) => getComputedStyle(input).fontSize),
                    credentialPlaceholderFontSizes: credentialInputs.map((input) =>
                        getComputedStyle(input, '::placeholder').fontSize
                    ),
                    copyIconMask: getComputedStyle(action.querySelector('.agent-response-copy-icon')).maskImage,
                    headingToken: token('--font-ui-lg'),
                    markerToken: token('--font-ui-lg'),
                    bodyToken: token('--font-ui-md'),
                    effectBleedToken: token('--layout-physical-effect-bleed'),
                    paragraphCounts: steps.map((step) => step.querySelectorAll('.agent-tunnel-step-copy p').length),
                    actionPackageParagraphCounts: actionPackages.map((pack) =>
                        pack.querySelectorAll('p').length
                    ),
                    credentialCopyChildren: [...credentialCopy.children].map((child) => child.tagName),
                    credentialFieldChildren: [...credentialFields.children].map((child) => child.tagName),
                    credentialSubstepMarkers: [...steps[1].querySelectorAll('.agent-tunnel-substep-number')].map(
                        (marker) => marker.textContent.trim()
                    ),
                    kickoffSubstepMarkers: [...steps[3].querySelectorAll('.agent-tunnel-substep-number')].map(
                        (marker) => marker.textContent.trim()
                    ),
                    kickoffActionGroups: [...steps[3].querySelectorAll('.agent-tunnel-numbered-item')].map(
                        (item) => [...item.querySelectorAll('.agent-tunnel-step-action')].map(
                            (element) => element.textContent.trim()
                        )
                    ),
                    substepMarkerFontSizes: [...steps[1].querySelectorAll('.agent-tunnel-substep-number'),
                        ...steps[3].querySelectorAll('.agent-tunnel-substep-number')].map(
                        (marker) => parseFloat(getComputedStyle(marker).fontSize)
                    ),
                    credentialToggleCount: steps[1].querySelectorAll('[data-agent-tunnel-toggle]').length,
                    stepFourActionHeights: [stepFourLink, stepFourButton].map((element) =>
                        element.getBoundingClientRect().height
                    ),
                    stepFourActionLabels: [...steps[3].querySelectorAll('.agent-tunnel-step-action')].map(
                        (element) => element.textContent.trim()
                    ),
                    kickoffHasExplicitBreak: Boolean(kickoff.querySelector('br')),
                    kickoffText: kickoff.value,
                    kickoffTagName: kickoff.tagName,
                    kickoffReadOnly: kickoff.readOnly,
                    kickoffDisabled: kickoff.disabled,
                    kickoffPaddingBlock: [
                        getComputedStyle(kickoff).paddingTop,
                        getComputedStyle(kickoff).paddingBottom,
                    ],
                    kickoffMaxHeight: getComputedStyle(kickoff).maxHeight,
                    kickoffTitleFontSize: getComputedStyle(
                        steps[3].querySelector('[data-agent-tunnel-kickoff-title]')
                    ).fontSize,
                    kickoffTitleFontWeight: getComputedStyle(
                        steps[3].querySelector('[data-agent-tunnel-kickoff-title]')
                    ).fontWeight,
                    guideTitleFontWeight: getComputedStyle(guideTitles[0]).fontWeight,
                    kickoffBorderRadius: getComputedStyle(kickoff).borderRadius,
                    kickoffBackgroundColor: getComputedStyle(kickoff).backgroundColor,
                    emptyCredentialBlocks: [...steps[1].querySelectorAll('div, p, span')].filter(
                        (element) => !element.children.length && !element.textContent.trim()
                            && !element.matches('[aria-hidden="true"], .icon')
                            && element.getBoundingClientRect().height > 0
                    ).length,
                    dividers: steps.map((step) => getComputedStyle(step).borderBlockStartWidth),
                    focused: document.activeElement === action,
                    focusedActionFocusVisible: action.matches(':focus-visible'),
                    focusedActionBoxShadow: actionStyle.boxShadow,
                    focusedActionOutlineStyle: actionStyle.outlineStyle,
                    focusedActionInsideClip:
                        actionRect.top >= clip.top
                        && actionRect.bottom <= clip.bottom
                        && actionRect.left >= clip.left
                        && actionRect.right <= clip.right,
                    focusRingRoom: Math.min(
                        clip.right - actionRect.right,
                        actionRect.left - clip.left,
                    ),
                    focusedActionBottomClearance: clip.bottom - actionRect.bottom,
                };
            }"""
        )

        assert geometry["workspaceOverflowY"] == "auto"
        assert geometry["gridOverflowY"] == "visible"
        assert geometry["cardOverflowY"] == "visible"
        assert geometry["workspaceScrollPaddingBlockEnd"] == geometry["effectBleedToken"]
        assert geometry["gridScrollable"] is False
        assert geometry["cardScrollable"] is False
        if height <= 420:
            assert geometry["workspaceScrollable"] is True
            assert geometry["scrollOwners"][0] == "agent_workspace"
            assert set(geometry["scrollOwners"]) <= {
                "agent_workspace",
                "agent_tunnel_kickoff",
            }
        else:
            assert geometry["scrollOwners"] in ([], ["agent_workspace"])
        assert max(geometry["markerHeadingCenterDeltas"]) <= 1, geometry["markerHeadingCenterDeltas"]
        assert geometry["markerFontSizes"] == [geometry["markerToken"]]
        assert geometry["markerColors"] == geometry["headingColors"]
        assert geometry["markerTexts"] == ["Step 1", "Step 2", "Step 3", "Step 4"]
        assert geometry["guideMarkerTexts"] == ["➊", "➋", "➌", "➍", "➊", "➋"]
        assert geometry["guideMarkerFontSizes"] == geometry["guideTitleFontSizes"]
        assert geometry["guideSummaryTexts"] == [
            "Create a tunnel",
            "Copy the Tunnel ID",
            "Create an API key",
            "Copy the secret key",
            "Enable Developer mode",
            "Create the AgenticContext plugin",
        ]
        assert geometry["guideDetailsOpen"] == [True] * 6
        assert geometry["guideSummaryDisplay"] == ["grid"]
        assert all("data:image/svg+xml" in mask for mask in geometry["guideSummaryMarkerMasks"])
        assert geometry["guideSummaryStyles"] == [
            {
                "columnGap": "8px",
                "padding": ["10px", "0px", "10px", "0px"],
                "iconSize": ["12px", "8px"],
                "bodyPadding": ["0px", "10px", "10px", "10px"],
            }
        ] * 6
        assert all(style["overflowX"] == "visible" for style in geometry["guideBodyStyles"])
        assert all(style["overflowY"] == "visible" for style in geometry["guideBodyStyles"])
        assert all(style["tabIndex"] == -1 for style in geometry["guideBodyStyles"])
        assert all(style["role"] == "region" for style in geometry["guideBodyStyles"])
        assert all(style["labelledBy"] for style in geometry["guideBodyStyles"])
        assert not any(style["horizontallyScrollable"] for style in geometry["guideBodyStyles"])
        assert all(width <= 640 for width in geometry["guideSvgClientWidths"])
        assert all(
            svg_width <= body["clientWidth"] + 1
            for svg_width, body in zip(
                geometry["guideSvgClientWidths"],
                geometry["guideBodyStyles"],
                strict=True,
            )
        )
        assert all(view_box["width"] == 640 for view_box in geometry["guideSvgViewBoxes"])
        assert all(view_box["height"] <= 1024 for view_box in geometry["guideSvgViewBoxes"])
        assert geometry["guideSvgStyles"] == [
            {"borderRadius": "10px", "overflow": "hidden"}
        ] * 6
        assert geometry["tunnelListCopyIconSize"]["height"] < geometry["tunnelListHeaderButtonHeight"]
        assert geometry["tunnelListCopyIconSize"]["width"] <= 20
        assert all(geometry["guideScrollportsInsideStep"])
        assert geometry["guideStepNonOverlapping"] is True
        assert max(geometry["actionPackageRightDeltas"]) <= 1
        assert all(geometry["actionPackagesFollowCopy"])
        assert geometry["actionLabels"] == [
            "Open Tunnels",
            "Open API keys",
            "Open ChatGPT",
            "Open Plugins",
            "Copy this prompt",
            "Ask in ChatGPT",
        ]
        assert all(
            "secondary-button" in class_name
            for class_name in geometry["actionClasses"]
        )
        assert geometry["headingFontSizes"] == [geometry["headingToken"]]
        assert geometry["bodyFontSizes"] == []
        assert len(set(geometry["credentialInputFontSizes"])) == 1
        assert geometry["credentialPlaceholderFontSizes"] == geometry["credentialInputFontSizes"]
        assert "document.on.document.fill.svg" in geometry["copyIconMask"]
        assert geometry["bodyToken"] < geometry["headingToken"]
        assert geometry["paragraphCounts"] == [0, 0, 0, 0]
        assert geometry["actionPackageParagraphCounts"] == [0]
        assert geometry["credentialCopyChildren"] == ["H4", "OL"]
        assert geometry["credentialFieldChildren"] == ["LI", "LI"]
        assert geometry["credentialSubstepMarkers"] == ["➊", "➋"]
        assert geometry["kickoffSubstepMarkers"] == ["➊", "➋"]
        assert geometry["kickoffActionGroups"] == [["Copy this prompt"], ["Ask in ChatGPT"]]
        assert geometry["substepMarkerFontSizes"] == [geometry["headingToken"]] * 4
        assert geometry["credentialToggleCount"] == 0
        assert max(geometry["stepFourActionHeights"]) - min(geometry["stepFourActionHeights"]) <= 1
        assert geometry["stepFourActionHeights"] == [32, 32]
        assert geometry["stepFourActionLabels"] == ["Copy this prompt", "Ask in ChatGPT"]
        assert geometry["kickoffHasExplicitBreak"] is False
        assert geometry["kickoffText"].endswith(
            "without altering unrelated work:\n[describe your task]."
        )
        assert geometry["kickoffTagName"] == "TEXTAREA"
        assert geometry["kickoffReadOnly"] is False
        assert geometry["kickoffDisabled"] is False
        assert geometry["kickoffPaddingBlock"] == ["8px", "8px"]
        assert geometry["kickoffMaxHeight"] == "96px"
        assert float(geometry["kickoffTitleFontSize"].removesuffix("px")) == geometry["headingToken"]
        assert geometry["kickoffTitleFontWeight"] == geometry["guideTitleFontWeight"]
        assert geometry["kickoffBorderRadius"] == "10px"
        assert geometry["kickoffBackgroundColor"] != "rgba(0, 0, 0, 0)"
        assert geometry["emptyCredentialBlocks"] == 0
        assert geometry["dividers"] == ["0px", "0px", "0px", "0px"]
        assert geometry["focused"] is True
        assert geometry["focusedActionFocusVisible"] is True
        assert (
            geometry["focusedActionBoxShadow"] != "none"
            or geometry["focusedActionOutlineStyle"] != "none"
        )
        assert geometry["focusedActionInsideClip"] is True
        assert geometry["focusRingRoom"] >= 4
        assert geometry["effectBleedToken"] >= 48
        assert geometry["focusedActionBottomClearance"] >= geometry["effectBleedToken"]
        assert geometry["titleOverlapsQuickActions"] is False
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert errors == []
    finally:
        context.close()


def test_tunnel_copy_failure_is_reported_without_false_success(
    disposable_browser,
    sidebar_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1280, "height": 900})
    context.add_init_script("""Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: {writeText: async () => { throw new Error('Unavailable'); }}
    }); document.execCommand = () => false;""")
    page = context.new_page()
    page.route(
        "**/api/agent/tunnel/status?platform=chatgpt",
        lambda route: route.fulfill(json=_tunnel_onboarding_status()),
    )
    try:
        page.goto(sidebar_server_url + "/agent/tunnel/chatgpt")
        onboarding = page.locator('[data-agent-tunnel-provider-panel="chatgpt"]')
        button = onboarding.locator("[data-agent-tunnel-copy-kickoff]")
        button.click()
        expect(button).to_have_text("Copy failed")
        expect(button).to_be_enabled()
        expect(onboarding.locator("[data-agent-tunnel-kickoff]")).to_be_visible()
    finally:
        context.close()


def test_tunnel_saved_key_is_only_a_mask_and_not_resubmitted(
    disposable_browser,
    sidebar_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    submitted = []
    page.route(
        "**/api/agent/tunnel/status?platform=chatgpt",
        lambda route: route.fulfill(json=_tunnel_onboarding_status()),
    )

    def save(route):
        submitted.append(route.request.post_data_json)
        route.fulfill(json=_tunnel_onboarding_status())

    page.route("**/api/agent/tunnel/credentials", save)
    try:
        page.goto(sidebar_server_url + "/agent/tunnel/chatgpt")
        key = page.locator("#chatgpt_tunnel_api_key")
        expect(key).to_have_attribute("placeholder", "••••••••ABCD")
        expect(key).to_have_value("")
        expect(key).to_have_attribute("type", "password")
        with page.expect_response("**/api/agent/tunnel/credentials"):
            page.locator("#chatgpt_tunnel_id").fill(TUNNEL_ONBOARDING_ID)
        # A successful save leaves no extra status copy behind.
        expect(page.locator("[data-agent-tunnel-hint]")).to_be_hidden()
        expect(page.locator("[data-agent-tunnel-key-check]")).to_be_visible()
        assert submitted and all(item["api_key"] == "" for item in submitted)
        key.fill(TUNNEL_ONBOARDING_KEY)
        page.wait_for_function("() => document.querySelector('#chatgpt_tunnel_api_key').value === ''")
        assert submitted[-1]["api_key"] == TUNNEL_ONBOARDING_KEY
        assert TUNNEL_ONBOARDING_KEY not in page.content()
        storage = page.evaluate("JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})")
        assert TUNNEL_ONBOARDING_KEY not in storage
    finally:
        context.close()


def test_tunnel_credential_errors_use_the_sidebar_hint_not_step_copy(
    disposable_browser,
    sidebar_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(
        "**/api/agent/tunnel/status?platform=chatgpt",
        lambda route: route.fulfill(json=_tunnel_onboarding_status()),
    )
    page.route(
        "**/api/agent/tunnel/credentials",
        lambda route: route.fulfill(status=400, json={"error": "Enter the API key for this Tunnel."}),
    )
    try:
        page.goto(sidebar_server_url + "/agent/tunnel/chatgpt")
        hint = page.locator("[data-agent-tunnel-hint]")
        tunnel_id = page.locator("#chatgpt_tunnel_id")
        expect(hint).to_be_hidden()
        expect(hint).to_have_attribute("aria-live", "polite")

        tunnel_id.fill("tunnel_short")
        expect(hint).to_be_visible()
        expect(hint).to_contain_text("Tunnel ID must be tunnel_")
        expect(tunnel_id).to_have_attribute("aria-invalid", "true")

        tunnel_id.fill(TUNNEL_ONBOARDING_ID)
        expect(tunnel_id).to_have_attribute("aria-invalid", "false")
        expect(hint).to_have_text("Enter the API key for this Tunnel.")
        onboarding = page.locator('[data-agent-tunnel-provider-panel="chatgpt"]')
        assert onboarding.locator(".agent-tunnel-credential-step p").count() == 0
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_gemini_tunnel_switch_config_copy_and_secret_dom_boundary(
    disposable_browser,
    sidebar_server_url,
    width,
):
    """Keep provider state, copied secrets, and callback approval explicitly separated."""
    secret = "gts_frontend_secret_DO_NOT_RENDER"
    callback_uri = (
        "https://consumer-callback.example/oauth/callback"
        "?flow=agentic%20context&return=%2Fapps"
    )
    state = {
        "origin": "https://agent.example.com",
        "authorization": {
            "pending": True,
            "review_id": "review_frontend_0123456789abcdef",
            "redirect_uri": callback_uri,
            "redirect_host": "consumer-callback.example",
            "expires_in": 599,
        },
    }
    config_requests = []
    copy_requests = []
    authorization_requests = []
    gemini_status_requests = []
    context = disposable_browser.new_context(viewport={"width": width, "height": 900})
    context.add_init_script(
        """Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: {writeText: async (value) => {
                if (window.__rejectClipboard) throw new Error('Unavailable');
                window.__copiedValues.push(value);
            }}
        });
        window.__copiedValues = [];
        window.__domCopyValues = [];
        window.__execCommandCalls = 0;
        window.__rejectClipboard = false;
        const nativeAppend = Element.prototype.append;
        Element.prototype.append = function (...nodes) {
            nodes.forEach((node) => {
                if (node instanceof HTMLTextAreaElement) {
                    window.__domCopyValues.push(node.value);
                }
            });
            return nativeAppend.apply(this, nodes);
        };
        document.execCommand = () => {
            window.__execCommandCalls += 1;
            return true;
        };"""
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(
        "**/api/agent/tunnel/status?platform=chatgpt",
        lambda route: route.fulfill(json={
            **_tunnel_onboarding_status(),
            "platform": "chatgpt",
        }),
    )
    def gemini_status(route):
        gemini_status_requests.append(route.request.url)
        route.fulfill(json=_gemini_tunnel_status(
            state["origin"],
            state["authorization"],
        ))

    page.route("**/api/agent/tunnel/status?platform=gemini", gemini_status)

    def save_config(route):
        payload = route.request.post_data_json
        config_requests.append(payload)
        state["origin"] = payload["public_origin"]
        route.fulfill(json=_gemini_tunnel_status(
            state["origin"],
            state["authorization"],
        ))

    def copy_value(route):
        payload = route.request.post_data_json
        copy_requests.append(payload)
        values = {
            "mcp_url": f'{state["origin"]}/mcp/gemini',
            "client_id": "gtc_frontend_test",
            "client_secret": secret,
        }
        route.fulfill(json={"value": values[payload["value"]]})

    def decide_authorization(route):
        authorization_requests.append(route.request.post_data_json)
        state["authorization"] = _empty_gemini_authorization()
        route.fulfill(json={"ok": True})

    page.route("**/api/agent/tunnel/gemini/config", save_config)
    page.route("**/api/agent/tunnel/gemini/copy-value", copy_value)
    page.route(
        "**/api/agent/tunnel/gemini/authorization",
        decide_authorization,
    )
    try:
        page.goto(sidebar_server_url + "/agent/tunnel/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        chatgpt_panel = page.locator('[data-agent-tunnel-provider-panel="chatgpt"]')
        gemini_panel = page.locator('[data-agent-tunnel-provider-panel="gemini"]')
        expect(chatgpt_panel).to_be_visible()
        expect(gemini_panel).to_be_hidden()

        page.get_by_role("button", name="Web service: ChatGPT", exact=True).click()
        page.get_by_role("option", name="Gemini", exact=True).click()
        if width < 900:
            page.locator("#sidebar_toggle").click()
        expect(page).to_have_url(re.compile(r"/agent/tunnel/gemini$"))
        expect(page.locator("[data-agent-heading]")).to_have_text(
            "Connect Gemini to this local project"
        )
        expect(chatgpt_panel).to_be_hidden()
        expect(gemini_panel).to_be_visible()
        expect(page.locator("[data-agent-tunnel-state]")).to_have_text("Configured")
        expect(page.locator("[data-agent-tunnel-status]")).to_have_attribute(
            "data-agent-tunnel-ready", "false"
        )
        expect(page.locator("[data-agent-tunnel-checkmark]")).to_be_hidden()
        expect(page.locator("[data-agent-tunnel-spinner]")).to_be_hidden()
        guides = gemini_panel.locator("details[data-agent-tunnel-guide]")
        expect(guides).to_have_count(4)
        assert guides.evaluate_all("nodes => nodes.map((node) => node.open)") == [False] * 4
        if width < 900:
            guides.first.locator("summary").click()
            guide_scrollport = guides.first.locator("[data-agent-tunnel-guide-scroll]")
            expect(guide_scrollport).to_be_visible()
            assert guide_scrollport.evaluate(
                "element => element.scrollWidth > element.clientWidth"
            )
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

        authorization = gemini_panel.locator("[data-agent-gemini-authorization]")
        redirect_uri = authorization.locator("[data-agent-gemini-redirect-uri]")
        redirect_host = authorization.locator("[data-agent-gemini-redirect-host]")
        expiry = authorization.locator("[data-agent-gemini-authorization-expiry]")
        approve = authorization.locator(
            '[data-agent-gemini-authorization-action="approve"]'
        )
        deny = authorization.locator(
            '[data-agent-gemini-authorization-action="deny"]'
        )
        expect(authorization).to_be_visible()
        expect(redirect_uri).to_have_value(callback_uri)
        expect(redirect_host).to_have_text("consumer-callback.example")
        expect(expiry).to_have_text("599 seconds")
        expect(approve).to_have_text("Approve callback")
        expect(deny).to_have_text("Deny")
        expect(approve).to_be_enabled()
        expect(deny).to_be_enabled()

        secret_button = gemini_panel.locator(
            '[data-agent-gemini-copy-value="client_secret"]'
        )
        secret_button.click()
        expect(secret_button).to_contain_text("Copied")
        assert page.evaluate("window.__copiedValues.at(-1)") == secret
        assert copy_requests == [{"value": "client_secret"}]
        assert authorization_requests == []
        expect(authorization).to_be_visible()
        expect(redirect_uri).to_have_value(callback_uri)

        authorization_action = "approve" if width >= 900 else "deny"
        authorization_button = approve if authorization_action == "approve" else deny
        status_request_count = len(gemini_status_requests)
        with page.expect_response("**/api/agent/tunnel/gemini/authorization"):
            with page.expect_response(
                "**/api/agent/tunnel/status?platform=gemini"
            ):
                authorization_button.click()
        assert authorization_requests == [{
            "action": authorization_action,
            "review_id": "review_frontend_0123456789abcdef",
        }]
        assert len(gemini_status_requests) > status_request_count
        expect(authorization).to_be_hidden()
        expect(redirect_uri).to_have_value("")
        expect(redirect_host).to_have_text("")
        expect(expiry).to_have_text("")
        page.wait_for_timeout(1_700)

        origin = gemini_panel.locator("[data-agent-gemini-public-origin]")
        expect(origin).to_have_value("https://agent.example.com")
        origin.fill("https://new.example.com")
        expect(gemini_panel.locator('[data-agent-gemini-copy-value="client_secret"]')).to_be_disabled()
        with page.expect_response("**/api/agent/tunnel/gemini/config"):
            gemini_panel.locator("[data-agent-gemini-save-origin]").click()
        assert config_requests == [{"public_origin": "https://new.example.com"}]
        expect(origin).to_have_value("https://new.example.com")

        expected_values = {
            "mcp_url": "https://new.example.com/mcp/gemini",
            "client_id": "gtc_frontend_test",
            "client_secret": secret,
        }
        for kind, expected in expected_values.items():
            button = gemini_panel.locator(f'[data-agent-gemini-copy-value="{kind}"]')
            expect(button).to_be_enabled()
            button.click()
            expect(button).to_contain_text("Copied")
            assert page.evaluate("window.__copiedValues.at(-1)") == expected
        assert copy_requests == [
            {"value": "client_secret"},
            *({"value": kind} for kind in expected_values),
        ]
        assert authorization_requests == [{
            "action": authorization_action,
            "review_id": "review_frontend_0123456789abcdef",
        }]
        assert secret not in page.content()
        storage = page.evaluate(
            "JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})"
        )
        assert secret not in storage

        page.wait_for_timeout(1_700)
        page.evaluate("window.__rejectClipboard = true")
        secret_button.click()
        expect(secret_button).to_contain_text("Copy failed")
        assert page.evaluate("window.__domCopyValues") == []
        assert page.evaluate("window.__execCommandCalls") == 0
        assert secret not in page.content()
        assert authorization_requests == [{
            "action": authorization_action,
            "review_id": "review_frontend_0123456789abcdef",
        }]

        with page.expect_response("**/api/agent/tunnel/gemini/config"):
            gemini_panel.locator("[data-agent-gemini-clear-origin]").click()
        assert config_requests == [
            {"public_origin": "https://new.example.com"},
            {"public_origin": ""},
        ]
        expect(origin).to_have_value("")
        expect(page.locator("[data-agent-tunnel-state]")).to_have_text("Not configured")
        expect(secret_button).to_be_disabled()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

        if width < 900:
            page.locator("#sidebar_toggle").click()
        page.get_by_role("button", name="Web service: Gemini", exact=True).click()
        page.get_by_role("option", name="Grok", exact=True).click()
        expect(page.locator("[data-agent-heading]")).to_have_text(
            "Connect Grok to this local project"
        )
        expect(chatgpt_panel).to_be_hidden()
        expect(gemini_panel).to_be_hidden()
        unsupported = page.locator("[data-agent-tunnel-unsupported]")
        expect(unsupported).to_be_visible()
        expect(unsupported).to_contain_text("Grok Tunnel is not available")
        expect(unsupported).to_contain_text("Choose ChatGPT or Gemini")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert errors == []
    finally:
        context.close()


def test_gemini_tunnel_ignores_a_late_chatgpt_status_response(
    disposable_browser,
    sidebar_server_url,
):
    """A superseded provider response cannot overwrite the active Gemini UI."""
    context = disposable_browser.new_context(viewport={"width": 1280, "height": 900})
    context.add_init_script(
        """const nativeFetch = window.fetch.bind(window);
        window.__tunnelStatusPending = {};
        window.__tunnelStatusRequests = [];
        window.__resolveTunnelStatus = (platform, payload) => {
            const resolve = window.__tunnelStatusPending[platform];
            if (!resolve) throw new Error(`No pending ${platform} Tunnel status request.`);
            delete window.__tunnelStatusPending[platform];
            resolve(new Response(JSON.stringify(payload), {
                status: 200,
                headers: {'Content-Type': 'application/json'},
            }));
        };
        window.fetch = (input, options = {}) => {
            const value = typeof input === 'string' ? input : input.url;
            const url = new URL(value, location.href);
            if (url.pathname === '/api/agent/tunnel/status') {
                const platform = url.searchParams.get('platform');
                window.__tunnelStatusRequests.push(url.pathname + url.search);
                return new Promise((resolve) => {
                    window.__tunnelStatusPending[platform] = resolve;
                });
            }
            return nativeFetch(input, options);
        };"""
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(sidebar_server_url + "/agent/tunnel/chatgpt")
        page.wait_for_function("() => Boolean(window.__tunnelStatusPending.chatgpt)")
        page.get_by_role("button", name="Web service: ChatGPT", exact=True).click()
        page.get_by_role("option", name="Gemini", exact=True).click()
        page.wait_for_function("() => Boolean(window.__tunnelStatusPending.gemini)")

        page.evaluate(
            "payload => window.__resolveTunnelStatus('gemini', payload)",
            _gemini_tunnel_status("https://fresh.example.com"),
        )
        expect(page.locator("[data-agent-tunnel-state]")).to_have_text("Configured")
        expect(page.locator('[data-agent-tunnel-provider-panel="gemini"]')).to_be_visible()

        stale_chatgpt = {
            **_tunnel_onboarding_status(),
            "platform": "chatgpt",
            "presentation": {
                "tone": "error",
                "label": "Stale ChatGPT",
                "message": "This response is no longer current.",
                "hint": "",
                "action": None,
            },
        }
        page.evaluate(
            "payload => window.__resolveTunnelStatus('chatgpt', payload)",
            stale_chatgpt,
        )
        page.wait_for_timeout(100)
        expect(page.locator("[data-agent-tunnel-state]")).to_have_text("Configured")
        expect(page.locator("[data-agent-heading]")).to_have_text(
            "Connect Gemini to this local project"
        )
        assert page.evaluate("window.__tunnelStatusRequests")[:2] == [
            "/api/agent/tunnel/status?platform=chatgpt",
            "/api/agent/tunnel/status?platform=gemini",
        ]
        assert errors == []
    finally:
        context.close()


def test_grok_composer_snapshot_uses_one_rendered_provider_scoped_surface(
    disposable_browser,
):
    context = disposable_browser.new_context(viewport={"width": 900, "height": 700})
    page = context.new_page()
    try:
        page.set_content(
            """
            <main>
                <textarea style="display:none" aria-label="Ask Grok anything"></textarea>
                <section id="primary-composer">
                    <div contenteditable="true" role="textbox"
                         aria-label="Ask Grok anything"></div>
                    <button data-testid="chat-submit" aria-label="Send"></button>
                </section>
                <section class="feedback-panel">
                    <div contenteditable="true" role="textbox"
                         aria-label="Ask Grok anything"></div>
                </section>
            </main>
            """
        )
        assert grok_composer_snapshot(page) == {"count": 1}
        page.locator("main").evaluate(
            """main => {
                const duplicate = document.createElement('textarea');
                duplicate.setAttribute('aria-label', 'Ask Grok anything');
                main.append(duplicate);
            }"""
        )
        assert grok_composer_snapshot(page) == {"count": 2}
    finally:
        context.close()


@pytest.mark.parametrize(
    "viewport",
    (
        {"width": 1_007, "height": 1_497},
        {"width": 390, "height": 844},
    ),
    ids=("annotated-desktop", "narrow"),
)
def test_safari_selection_persists_for_source_only_providers_without_edge_fallback(
    disposable_browser,
    agent_selection_server_url,
    viewport,
):
    context = disposable_browser.new_context(viewport=viewport)
    page = context.new_page()
    browser_status_requests = []
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.add_init_script(
        """(() => {
            window.__agentRouteWrites = [];
            const originalReplaceState = history.replaceState.bind(history);
            history.replaceState = (...args) => {
                window.__agentRouteWrites.push(String(args[2] || ''));
                return originalReplaceState(...args);
            };
        })()"""
    )
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)

    def fulfill_agent_status(route):
        headers = route.request.headers
        platform = headers.get("x-cachelikes-agent-platform", "chatgpt")
        browser = headers.get("x-cachelikes-agent-browser", "edge")
        route.fulfill(
            json={
                **base,
                "agent": {
                    "session_id": headers.get("x-cachelikes-agent-session", "new"),
                    "platform": platform,
                    "browser": browser,
                    "running": False,
                    "phase": "idle",
                    "history": [],
                },
                "sessions": [],
                "active_count": 0,
                "can_start": True,
            }
        )

    def fulfill_browser_status(route):
        query = parse_qs(urlsplit(route.request.url).query)
        platform = query.get("platform", [""])[0]
        browser = query.get("browser", [""])[0]
        platform_label = {
            "chatgpt": "ChatGPT",
            "gemini": "Gemini",
            "grok": "Grok",
            "claude": "Claude",
        }[platform]
        conversation_url = {
            "chatgpt": "https://chatgpt.com/c/safari-recent",
            "gemini": "https://gemini.google.com/app/safari-recent",
            "grok": "https://grok.com/c/safari-recent",
            "claude": "https://claude.ai/chat/safari-recent",
        }[platform]
        browser_status_requests.append((platform, browser))
        route.fulfill(
            json={
                "platform": platform,
                "browser": browser,
                "browser_label": browser.title(),
                "logged_in": True,
                "can_download": True,
                "account_name": "Signed in",
                "message": "Ready",
                "agent_sources": {
                    "recent_sessions": [{
                        "id": "safari-recent",
                        "title": f"{platform_label} Safari recent session",
                        "url": conversation_url,
                        "updated_at": "2026-09-14T00:00:00Z",
                    }],
                    "projects": [],
                },
                "agent_execution_supported": (
                    browser != "safari" or platform in {"chatgpt", "grok"}
                ),
                "agent_execution_message": (
                    ""
                    if browser != "safari" or platform in {"chatgpt", "grok"}
                    else f"Safari can browse {platform.title()} Recent sessions here."
                ),
            }
        )

    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(
            json={"platform": "grok", "recent_sessions": [], "projects": []}
        ),
    )

    def ensure_sidebar_open():
        if viewport["width"] > 900:
            return
        shell = page.locator(".app-shell")
        if "is-sidebar-open" not in (shell.get_attribute("class") or "").split():
            page.get_by_role("button", name="Toggle sidebar", exact=True).click()
            expect(shell).to_have_class(re.compile(r"\bis-sidebar-open\b"))

    def ensure_sidebar_closed():
        if viewport["width"] > 900:
            return
        shell = page.locator(".app-shell")
        if "is-sidebar-open" in (shell.get_attribute("class") or "").split():
            page.get_by_role("button", name="Toggle sidebar", exact=True).click()
            expect(shell).to_have_class(re.compile(r"\bis-sidebar-collapsed\b"))

    try:
        page.goto(f"{agent_selection_server_url}/agent/edge/chatgpt")
        ensure_sidebar_open()
        page.get_by_role("button", name="Web service: ChatGPT", exact=True).click()
        page.get_by_role("option", name="Grok", exact=True).click()
        page.get_by_role("button", name="Browser: Edge", exact=True).click()
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("platform") == "grok"
            and response.request.post_data_json.get("browser") == "safari"
        ) as preference_response:
            page.get_by_role("option", name="Safari", exact=True).click()

        saved_payload = preference_response.value.request.post_data_json
        assert saved_payload["platform"] == "grok"
        assert saved_payload["browser"] == "safari"
        assert saved_payload["model"] == "grok-build"
        assert saved_payload["preference_client_id"]
        assert saved_payload["preference_revision"] >= 1
        assert not page_errors
        assert page.evaluate(
            """() => ({
                platform: document.querySelector('.agent-platform-combobox input')?.value,
                browser: document.querySelector('.agent-browser-combobox input')?.value,
                path: location.pathname,
                routeWrites: window.__agentRouteWrites,
            })"""
        ) == {
            "platform": "grok",
            "browser": "safari",
            "path": "/agent/safari/grok",
            "routeWrites": ["/agent/edge/grok", "/agent/safari/grok"],
        }
        expect(page).to_have_url(f"{agent_selection_server_url}/agent/safari/grok")
        expect(
            page.locator('#agent_runtime_form input[name="platform"]')
        ).to_have_value("grok")
        expect(page.locator('#agent_runtime_form input[name="browser"]')).to_have_value(
            "safari"
        )
        expect(page.locator(".agent-execution-session-title")).to_have_text(
            "Grok Safari recent session"
        )

        ensure_sidebar_closed()
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("platform") == "grok"
            and response.request.post_data_json.get("browser") == "safari"
            and response.request.post_data_json.get("model") == "grok-auto"
        ) as model_preference_response:
            page.get_by_role("button", name="Model: Build", exact=True).click()
            page.get_by_role("option", name="Grok · Auto", exact=True).click()

        model_payload = model_preference_response.value.request.post_data_json
        assert model_payload["model"] == "grok-auto"
        assert model_payload["preference_client_id"] == saved_payload[
            "preference_client_id"
        ]
        assert model_payload["preference_revision"] > saved_payload[
            "preference_revision"
        ]
        expect(page.get_by_role("button", name="Model: Auto", exact=True)).to_be_visible()

        page.reload(wait_until="domcontentloaded")
        ensure_sidebar_open()
        expect(page.get_by_role("button", name="Web service: Grok", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Browser: Safari", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Model: Auto", exact=True)).to_be_visible()
        page.goto(f"{agent_selection_server_url}/agent", wait_until="domcontentloaded")
        ensure_sidebar_open()
        expect(page).to_have_url(f"{agent_selection_server_url}/agent/tunnel/grok")
        page.locator('label[for="agent_connection_browser"]').click()
        expect(page).to_have_url(f"{agent_selection_server_url}/agent/safari/grok")

        page.wait_for_function(
            "() => document.querySelector('[data-role=browser-session-account]')?.textContent === 'Signed in'"
        )
        browser_status_requests.clear()
        page.get_by_role("button", name="Web service: Grok", exact=True).click()
        with page.expect_response(
            lambda response: "/api/browser-session?" in response.url
            and "platform=gemini" in response.url
            and "browser=safari" in response.url
        ), page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("platform") == "gemini"
            and response.request.post_data_json.get("browser") == "safari"
        ) as reloaded_preference_response:
            page.get_by_role("option", name="Gemini", exact=True).click()
        reloaded_payload = reloaded_preference_response.value.request.post_data_json
        assert reloaded_payload["preference_client_id"] == saved_payload[
            "preference_client_id"
        ]
        assert reloaded_payload["preference_revision"] > saved_payload[
            "preference_revision"
        ]
        expect(page).to_have_url(f"{agent_selection_server_url}/agent/safari/gemini")
        expect(page.get_by_role("button", name="Browser: Safari", exact=True)).to_be_visible()
        expect(page.locator("#agent_ask_button")).to_be_disabled()
        expect(page.locator(".agent-execution-session-title")).to_have_text(
            "Gemini Safari recent session"
        )
        assert "Safari can browse Gemini Recent sessions here" in (
            page.locator("#agent_ask_button").get_attribute("title") or ""
        )
        assert ("gemini", "safari") in browser_status_requests
        assert ("gemini", "edge") not in browser_status_requests

        browser_status_requests.clear()
        page.get_by_role("button", name="Web service: Gemini", exact=True).click()
        with page.expect_response(
            lambda response: "/api/browser-session?" in response.url
            and "platform=claude" in response.url
            and "browser=safari" in response.url
        ), page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("platform") == "claude"
            and response.request.post_data_json.get("browser") == "safari"
        ):
            page.get_by_role("option", name="Claude", exact=True).click()
        expect(page).to_have_url(f"{agent_selection_server_url}/agent/safari/claude")
        expect(page.get_by_role("button", name="Browser: Safari", exact=True)).to_be_visible()
        expect(page.locator("#agent_ask_button")).to_be_disabled()
        expect(page.locator(".agent-execution-session-title")).to_have_text(
            "Claude Safari recent session"
        )
        assert ("claude", "safari") in browser_status_requests
        assert ("claude", "edge") not in browser_status_requests

        browser_status_requests.clear()
        page.get_by_role("button", name="Web service: Claude", exact=True).click()
        with page.expect_response(
            lambda response: "/api/browser-session?" in response.url
            and "platform=chatgpt" in response.url
            and "browser=safari" in response.url
        ), page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("platform") == "chatgpt"
            and response.request.post_data_json.get("browser") == "safari"
        ):
            page.get_by_role("option", name="ChatGPT", exact=True).click()
        expect(page).to_have_url(f"{agent_selection_server_url}/agent/safari/chatgpt")
        expect(page.locator("#agent_ask_button")).to_be_enabled()
        expect(page.locator(".agent-execution-session-title")).to_have_text(
            "ChatGPT Safari recent session"
        )
        assert ("chatgpt", "safari") in browser_status_requests
        assert ("chatgpt", "edge") not in browser_status_requests
    finally:
        context.close()


def test_agent_project_path_input_survives_an_immediate_reload(
    disposable_browser,
    agent_selection_server_url,
    tmp_path,
):
    """Persist a valid typed project path even when reload happens before blur."""
    remembered_project = tmp_path / "Remembered Project"
    remembered_project.mkdir()
    remembered_path = str(remembered_project)
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    try:
        page.goto(
            f"{agent_selection_server_url}/agent/tunnel/chatgpt",
            wait_until="domcontentloaded",
        )
        page.locator("#agent_project_path").evaluate(
            """(input, path) => {
                input.value = path;
                input.dispatchEvent(new Event('input', {bubbles: true}));
            }""",
            remembered_path,
        )
        expect(page.locator("[data-agent-project-name]")).to_have_text(
            remembered_project.name
        )

        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("workspace_path") == remembered_path
        ):
            page.reload(wait_until="domcontentloaded")

        expect(page.locator("#agent_project_path")).to_have_value(remembered_path)
        expect(page.locator('input[name="workspace_path"]')).to_have_value(
            remembered_path
        )
        expect(page.locator("[data-agent-project-name]")).to_have_text(
            remembered_project.name
        )

        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#agent_project_path")).to_have_value(remembered_path)
    finally:
        context.close()


def test_agent_preference_save_retries_an_unchanged_failed_payload(
    disposable_browser,
    agent_selection_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    requests = []
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)

    def fulfill_agent_status(route):
        headers = route.request.headers
        route.fulfill(
            json={
                **base,
                "agent": {
                    "session_id": "new",
                    "platform": headers.get("x-cachelikes-agent-platform", "chatgpt"),
                    "browser": headers.get("x-cachelikes-agent-browser", "edge"),
                    "running": False,
                    "phase": "idle",
                    "history": [],
                },
                "sessions": [],
                "active_count": 0,
                "can_start": True,
            }
        )

    def fulfill_preference(route):
        requests.append(route.request.post_data_json)
        if len(requests) == 1:
            route.fulfill(status=503, json={"error": "Temporary failure"})
            return
        route.fulfill(json={"settings": {}, "runtime": {}})

    page.route("**/api/agent/status", fulfill_agent_status)
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "platform": "grok",
                "browser": "safari",
                "browser_label": "Safari",
                "logged_in": True,
                "can_download": True,
                "account_name": "Signed in",
                "message": "Ready",
                "agent_sources": {"recent_sessions": [], "projects": []},
            }
        ),
    )
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(
            json={"platform": "grok", "recent_sessions": [], "projects": []}
        ),
    )
    page.route("**/api/agent/preferences", fulfill_preference)
    try:
        page.goto(f"{agent_selection_server_url}/agent/edge/chatgpt")
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
        ):
            page.get_by_role("button", name="Web service: ChatGPT", exact=True).click()
            page.get_by_role("option", name="Grok", exact=True).click()
        page.wait_for_function("() => window.location.pathname === '/agent/edge/grok'")
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
        ):
            page.wait_for_timeout(1_100)
        assert len(requests) >= 2
        assert requests[1] == requests[0]
        assert requests[1]["platform"] == "grok"
        assert requests[1]["browser"] == "edge"
    finally:
        context.close()


def test_agent_preference_save_newer_selection_supersedes_failed_payload(
    disposable_browser,
    agent_selection_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    requests = []
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)

    def fulfill_agent_status(route):
        headers = route.request.headers
        route.fulfill(
            json={
                **base,
                "agent": {
                    "session_id": "new",
                    "platform": headers.get("x-cachelikes-agent-platform", "chatgpt"),
                    "browser": headers.get("x-cachelikes-agent-browser", "edge"),
                    "running": False,
                    "phase": "idle",
                    "history": [],
                },
                "sessions": [],
                "active_count": 0,
                "can_start": True,
            }
        )

    def fulfill_preference(route):
        requests.append(route.request.post_data_json)
        if len(requests) == 1:
            route.fulfill(status=503, json={"error": "Temporary failure"})
            return
        route.fulfill(json={"settings": {}, "runtime": {}})

    page.route("**/api/agent/status", fulfill_agent_status)
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "platform": "grok",
                "browser": "safari",
                "browser_label": "Safari",
                "logged_in": True,
                "can_download": True,
                "account_name": "Signed in",
                "message": "Ready",
                "agent_sources": {"recent_sessions": [], "projects": []},
            }
        ),
    )
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(
            json={"platform": "grok", "recent_sessions": [], "projects": []}
        ),
    )
    page.route("**/api/agent/preferences", fulfill_preference)
    try:
        page.goto(f"{agent_selection_server_url}/agent/edge/chatgpt")
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
        ):
            page.get_by_role("button", name="Web service: ChatGPT", exact=True).click()
            page.get_by_role("option", name="Grok", exact=True).click()
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("browser") == "safari"
        ):
            page.get_by_role("button", name="Browser: Edge", exact=True).click()
            page.get_by_role("option", name="Safari", exact=True).click()
        page.wait_for_function("() => window.location.pathname === '/agent/safari/grok'")
        assert len(requests) >= 2
        assert requests[0]["platform"] == "grok"
        assert requests[0]["browser"] == "edge"
        assert requests[1]["platform"] == "grok"
        assert requests[1]["browser"] == "safari"
        assert requests[1]["preference_revision"] > requests[0]["preference_revision"]
    finally:
        context.close()


def test_agent_preference_outbox_restores_selection_and_replays_on_first_reload(
    disposable_browser,
    agent_selection_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    requests = []
    status_selections = []
    browser_selections = []
    base = fixtures._finished_chatgpt_agent_payload()
    base.update(agent={"session_id": "new"}, sessions=[], active_count=0, can_start=True)

    def fulfill_status(route):
        headers = route.request.headers
        status_selections.append(
            (
                headers.get("x-cachelikes-agent-platform"),
                headers.get("x-cachelikes-agent-browser"),
            )
        )
        route.fulfill(json=base)

    def fulfill_browser_status(route):
        query = parse_qs(urlsplit(route.request.url).query)
        browser_selections.append(
            (query.get("platform", [""])[0], query.get("browser", [""])[0])
        )
        route.fulfill(
            json={
                "platform": "grok",
                "browser": "safari",
                "browser_label": "Safari",
                "logged_in": True,
                "can_download": True,
                "account_name": "Signed in",
                "message": "Ready",
                "agent_sources": {"recent_sessions": [], "projects": []},
            }
        )

    page.route("**/api/agent/status", fulfill_status)
    page.route(
        "**/api/browser-session**",
        fulfill_browser_status,
    )
    page.route(
        "**/api/agent/preferences",
        lambda route: (
            requests.append(route.request.post_data_json),
            route.fulfill(json={"settings": {}, "runtime": {}}),
        ),
    )
    try:
        page.goto(f"{agent_selection_server_url}/agent/edge/chatgpt")
        payload = page.evaluate(
            """() => {
                const payload = {
                    workspace_path: document.querySelector('[name="workspace_path"]').value,
                    operating_system: 'macos',
                    platform: 'grok',
                    browser: 'safari',
                    model: 'grok-build',
                    chatgpt_effort: 'highest_available',
                    preference_client_id: 'outbox-reload-client',
                    preference_revision: 41,
                };
                sessionStorage.setItem(
                    'cachelikes:agent-preference-client-v1',
                    payload.preference_client_id,
                );
                sessionStorage.setItem(
                    'cachelikes:agent-preference-revision-v1',
                    String(payload.preference_revision),
                );
                sessionStorage.setItem(
                    'cachelikes:agent-preference-pending-v1',
                    JSON.stringify(payload),
                );
                return payload;
            }"""
        )

        status_selections.clear()
        browser_selections.clear()
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.location.pathname === '/agent/safari/grok'")
        expect(page.get_by_role("button", name="Web service: Grok", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Browser: Safari", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Model: Build", exact=True)).to_be_visible()
        page.wait_for_function("() => sessionStorage.getItem('cachelikes:agent-preference-pending-v1') === null")
        assert requests == [payload]
        assert status_selections[0] == ("grok", "safari")
        assert browser_selections[0] == ("grok", "safari")
    finally:
        context.close()


@pytest.mark.parametrize(
    "invalid_model",
    ("grok-retired", "latest_available"),
    ids=("removed-model", "wrong-platform-model"),
)
def test_agent_preference_outbox_discards_invalid_model_without_restoring_or_posting(
    disposable_browser,
    agent_selection_server_url,
    invalid_model,
):
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    requests = []
    base = fixtures._finished_chatgpt_agent_payload()
    base.update(agent={"session_id": "new"}, sessions=[], active_count=0, can_start=True)
    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "platform": "chatgpt",
                "browser": "edge",
                "browser_label": "Edge",
                "logged_in": True,
                "can_download": True,
                "account_name": "Signed in",
                "message": "Ready",
                "agent_sources": {"recent_sessions": [], "projects": []},
            }
        ),
    )
    page.route(
        "**/api/agent/preferences",
        lambda route: (
            requests.append(route.request.post_data_json),
            route.fulfill(status=409, json={"error": "Choose a supported Grok model."}),
        ),
    )
    try:
        page.goto(f"{agent_selection_server_url}/agent/edge/chatgpt")
        baseline = page.evaluate(
            """() => ({
                workspace_path: document.querySelector('[name="workspace_path"]').value,
                platform: document.querySelector('.agent-platform-combobox input').value,
                browser: document.querySelector('.agent-browser-combobox input').value,
                model: document.querySelector('[data-agent-model-input]').value,
            })"""
        )
        page.evaluate(
            """({workspacePath, model}) => {
                const payload = {
                    workspace_path: workspacePath,
                    operating_system: 'macos',
                    platform: 'grok',
                    browser: 'safari',
                    model,
                    chatgpt_effort: 'highest_available',
                    preference_client_id: 'invalid-outbox-client',
                    preference_revision: 51,
                };
                sessionStorage.setItem(
                    'cachelikes:agent-preference-client-v1',
                    payload.preference_client_id,
                );
                sessionStorage.setItem(
                    'cachelikes:agent-preference-revision-v1',
                    String(payload.preference_revision),
                );
                sessionStorage.setItem(
                    'cachelikes:agent-preference-pending-v1',
                    JSON.stringify(payload),
                );
            }""",
            {"workspacePath": baseline["workspace_path"], "model": invalid_model},
        )

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "() => sessionStorage.getItem('cachelikes:agent-preference-pending-v1') === null"
        )
        page.wait_for_timeout(1_250)
        assert requests == []
        assert page.evaluate(
            """() => ({
                platform: document.querySelector('.agent-platform-combobox input').value,
                browser: document.querySelector('.agent-browser-combobox input').value,
                model: document.querySelector('[data-agent-model-input]').value,
                path: window.location.pathname,
            })"""
        ) == {
            "platform": baseline["platform"],
            "browser": baseline["browser"],
            "model": baseline["model"],
            "path": "/agent/edge/chatgpt",
        }
    finally:
        context.close()


def test_removed_remembered_project_falls_back_to_new_without_request_loop(
    disposable_browser,
    sidebar_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.update(agent={"session_id": "new"}, sessions=[], active_count=0, can_start=True)
    project_session_requests = []
    page.add_init_script(
        """localStorage.setItem(
            'cachelikes:agent-session-selection:v1:chatgpt:edge',
            JSON.stringify({
                version: 1,
                mode: 'project',
                project_url: 'https://chatgpt.com/g/removed-project',
                project_session_url: 'https://chatgpt.com/c/removed-session',
            }),
        );"""
    )
    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "can_download": True,
                "logged_in": True,
                "browser": "edge",
                "platform": "chatgpt",
                "account_name": "Signed in",
                "agent_sources": {"recent_sessions": [], "projects": []},
            }
        ),
    )
    page.route(
        "**/api/agent/project-sessions**",
        lambda route: (
            project_session_requests.append(route.request.url),
            route.fulfill(json={"recent_sessions": []}),
        ),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        expect(
            page.get_by_role("button", name="Session source: New session", exact=True)
        ).to_be_visible()
        expect(page.get_by_role("button", name="Ask ChatGPT Web", exact=True)).to_be_enabled()
        remembered = page.evaluate(
            """JSON.parse(localStorage.getItem(
                'cachelikes:agent-session-selection:v1:chatgpt:edge'
            ))"""
        )
        assert remembered == {
            "version": 1,
            "mode": "new",
            "project_url": "",
            "project_session_url": "new",
        }
        page.reload(wait_until="domcontentloaded")
        expect(
            page.get_by_role("button", name="Session source: New session", exact=True)
        ).to_be_visible()
        expect(page.get_by_role("button", name="Ask ChatGPT Web", exact=True)).to_be_enabled()
        assert project_session_requests == []
    finally:
        context.close()


def test_force_recheck_bypasses_hanging_cached_request_and_keeps_newest_result(
    disposable_browser,
    sidebar_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.update(agent={"session_id": "new"}, sessions=[], active_count=0, can_start=True)
    held_requests = []
    source_requests = []

    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route("**/api/browser-session**", lambda route: held_requests.append(route))
    page.route(
        "**/api/agent/sources**",
        lambda route: (
            source_requests.append(route.request.url),
            route.abort(),
        ),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/grok")
        for _ in range(100):
            if len(held_requests) == 1:
                break
            page.wait_for_timeout(20)
        assert len(held_requests) == 1
        assert "refresh=1" not in held_requests[0].request.url

        page.locator('[data-role="browser-session-recheck"]').dispatch_event("click")
        for _ in range(100):
            if len(held_requests) == 2:
                break
            page.wait_for_timeout(20)
        assert len(held_requests) == 2
        assert "refresh=1" in held_requests[1].request.url

        held_requests[1].fulfill(
            json={
                "can_download": True,
                "logged_in": True,
                "browser": "edge",
                "platform": "grok",
                "account_name": "Fresh User",
                "message": "Fresh readiness",
                "agent_sources": {
                    "recent_sessions": [],
                    "projects": [
                        {"title": "Fresh Project", "url": "https://grok.com/project/fresh"}
                    ],
                },
            }
        )
        account = page.locator('[data-role="browser-session-account"]')
        expect(account).to_have_text("Fresh User")
        expect(
            page.locator(
                '[data-agent-session-list="projects"] '
                '[data-agent-combobox-option="https://grok.com/project/fresh"]'
            )
        ).to_have_count(1)

        held_requests[0].fulfill(
            json={
                "can_download": False,
                "logged_in": False,
                "browser": "edge",
                "platform": "grok",
                "account_name": "",
                "message": "Old readiness",
                "agent_sources": {
                    "recent_sessions": [],
                    "projects": [
                        {"title": "Old Project", "url": "https://grok.com/project/old"}
                    ],
                },
            }
        )
        page.wait_for_timeout(100)
        expect(account).to_have_text("Fresh User")
        expect(
            page.locator(
                '[data-agent-session-list="projects"] '
                '[data-agent-combobox-option="https://grok.com/project/fresh"]'
            )
        ).to_have_count(1)
        expect(
            page.locator(
                '[data-agent-session-list="projects"] '
                '[data-agent-combobox-option="https://grok.com/project/old"]'
            )
        ).to_have_count(0)
        assert source_requests == []
    finally:
        context.close()


@pytest.mark.parametrize(("width", "color_scheme"), [(1024, "light"), (390, "dark")])
def test_agent_response_renders_latex_without_treating_currency_as_math(
    disposable_browser, sidebar_server_url, width, color_scheme,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 959},
        color_scheme=color_scheme,
    )
    page = context.new_page()
    raw_response = (
        "Ordinary currency stays text: $36,646.96\n\n"
        r"\[$14,523.85-\text{已售成本}=$1,755.92\]"
        "\n\n"
        r"Inline \(x^2 + y^2\) and `\[literal code\]`."
    )
    base = fixtures._finished_chatgpt_agent_payload()
    base["agent"].update(
        response=raw_response,
        response_html=str(render_agent_response(raw_response)),
        history=[],
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": {"recent_sessions": [], "projects": []},
    }))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        answer = page.locator("#agent_response_answer")
        expect(answer.locator(".katex-display .katex")).to_have_count(1)
        expect(answer.locator(".katex")).to_have_count(2)
        expect(answer.locator(".katex-mathml math")).to_have_count(2)
        expect(answer.locator(".katex-display .katex-html")).to_contain_text("已售成本")
        expect(answer.locator("code")).to_have_text(r"\[literal code\]")
        expect(answer).to_contain_text("Ordinary currency stays text: $36,646.96")
        assert answer.locator(".katex-error").count() == 0
        geometry = answer.locator(".katex-display").evaluate(
            "element => ({clientWidth: element.clientWidth, scrollWidth: element.scrollWidth})",
        )
        assert geometry["clientWidth"] <= answer.evaluate("element => element.clientWidth")
        assert geometry["scrollWidth"] >= geometry["clientWidth"]
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize(
    ("width", "height"),
    ((1_007, 1_355), (390, 844)),
)
def test_grok_response_renders_native_markdown_without_provider_markup_leaks(
    disposable_browser,
    sidebar_server_url,
    width,
    height,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme="dark",
        reduced_motion="reduce",
    )
    page = context.new_page()
    raw_response = (
        "**Evidence-ranked longevity options**\n\n"
        "### 1. Rapamycin / Sirolimus\n\n"
        "- **Original use**: transplant immunosuppression.\n"
        "- **Evidence**: animal lifespan results are stronger than human outcomes. "
        '<grok:render card_id="066a7b" card_type="citation_card" '
        'type="render_inline_citation"><argument name="citation_id">81</argument>'
        "</grok:render>\n\n"
        "### 2. Evidence matrix\n\n"
        "| Drug | Original indication | Longevity discussion | Evidence and safety notes |\n"
        "| --- | --- | --- | --- |\n"
        "| Rapamycin | Transplant immunosuppression | Intermittent mTORC1 inhibition | "
        "Human lifespan endpoints remain unproven |\n"
        "| SGLT2 inhibitor | Type 2 diabetes | Cardiometabolic risk reduction | "
        "Benefits come from indicated clinical populations |"
    )
    citations = [
        {
            "card_id": "066a7b",
            "citation_id": "81",
            "url": "https://example.com/source",
            "label": "Example",
        }
    ]
    base = fixtures._finished_chatgpt_agent_payload()
    base["agent"].update(
        platform="grok",
        browser="edge",
        model="grok-build",
        actual_model="Grok Build",
        prompt="Compare longevity prescriptions.",
        response=raw_response,
        response_html=str(
            render_agent_response(
                raw_response,
                provider="grok",
                citations=citations,
            )
        ),
        history=[],
    )
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "can_download": True,
                "logged_in": True,
                "browser": "edge",
                "platform": "grok",
                "agent_sources": {"recent_sessions": [], "projects": []},
            }
        ),
    )
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(
            json={"platform": "grok", "recent_sessions": [], "projects": []}
        ),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/grok")
        answer = page.locator("#agent_response_answer")
        expect(answer).to_contain_text("Evidence-ranked longevity options")

        visible_text = answer.inner_text()
        rendered_markup = answer.inner_html()
        for provider_fragment in ("grok:render", "argument", "citation_id"):
            assert provider_fragment not in visible_text
            assert provider_fragment not in rendered_markup

        heading = answer.locator("h3").first
        paragraph = answer.locator("li").first
        strong = answer.locator("strong").first
        expect(heading).to_have_css("font-size", "20px")
        expect(heading).to_have_css("line-height", "28px")
        expect(strong).to_have_css("font-weight", "600")
        type_scale = answer.evaluate(
            """element => ({
                heading: parseFloat(getComputedStyle(element.querySelector('h3')).fontSize),
                body: parseFloat(getComputedStyle(element.querySelector('li')).fontSize),
            })"""
        )
        assert type_scale["heading"] > type_scale["body"]
        expect(paragraph).to_be_visible()

        citation = answer.locator("a.agent-inline-citation")
        expect(citation).to_have_count(1)
        expect(citation).to_have_text("Example")
        expect(citation).to_have_attribute("href", "https://example.com/source")
        expect(citation).to_have_attribute("target", "_blank")
        rel_tokens = set((citation.get_attribute("rel") or "").split())
        assert {"noopener", "noreferrer", "nofollow"}.issubset(rel_tokens)
        citation_style = citation.evaluate(
            """element => {
                const style = getComputedStyle(element);
                const bounds = element.getBoundingClientRect();
                return {
                    backgroundColor: style.backgroundColor,
                    borderRadius: parseFloat(style.borderRadius),
                    fontSize: style.fontSize,
                    paddingInlineEnd: parseFloat(style.paddingInlineEnd),
                    paddingInlineStart: parseFloat(style.paddingInlineStart),
                    height: bounds.height,
                    protocol: new URL(element.href).protocol,
                    whiteSpace: style.whiteSpace,
                };
            }"""
        )
        assert citation_style["protocol"] == "https:"
        assert citation_style["fontSize"] == "13px"
        assert citation_style["whiteSpace"] == "nowrap"
        assert citation_style["paddingInlineStart"] >= 7
        assert citation_style["paddingInlineEnd"] >= 7
        assert citation_style["borderRadius"] >= citation_style["height"] / 2
        assert citation_style["backgroundColor"] != "rgba(0, 0, 0, 0)"

        table_shell = answer.locator(".agent-markdown-table-shell")
        expect(table_shell).to_have_count(1)
        expect(table_shell.locator("table")).to_have_count(1)
        if width == 390:
            table_geometry = table_shell.evaluate(
                "element => ({clientWidth: element.clientWidth, scrollWidth: element.scrollWidth})"
            )
            assert table_geometry["scrollWidth"] > table_geometry["clientWidth"]
            table_shell.evaluate("element => { element.scrollLeft = element.scrollWidth; }")
            page.wait_for_function(
                """element => (
                    element.scrollLeft >= element.scrollWidth - element.clientWidth - 1
                )""",
                arg=table_shell.element_handle(),
            )

        document_geometry = page.evaluate(
            """() => ({
                clientWidth: document.documentElement.clientWidth,
                scrollWidth: document.documentElement.scrollWidth,
            })"""
        )
        assert document_geometry["scrollWidth"] <= document_geometry["clientWidth"] + 1
        assert not page_errors
    finally:
        context.close()


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
        if width < 900:
            page.locator("#sidebar_toggle").click()
        rail = page.locator("[data-agent-execution-sessions]")
        expect(page.locator('[data-agent-new-task]')).to_have_count(0)
        expect(page.locator('[data-agent-new-session]')).to_be_visible()
        expect(rail).to_have_js_property('open', True)
        summary = rail.locator('summary')
        summary.focus()
        summary.press('Enter')
        expect(rail).to_have_js_property('open', False)
        expect(page.locator('[data-agent-execution-session-list]')).to_be_hidden()
        summary.press('Space')
        expect(rail).to_have_js_property('open', True)
        expect(page.locator('[data-agent-execution-session-list]')).to_be_visible()

        expect(rail).to_be_visible()
        expect(rail.locator("[data-agent-session-capacity]")).to_have_text("2")
        row = rail.locator("[data-execution-session-id=primary]")
        expect(row).to_have_css("border-radius", "999px")
        assert abs(row.bounding_box()["height"] - 36) < 0.1
        assert rail.evaluate("e => Boolean(document.querySelector('#agent_runtime_form').compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING)")
        spinner = rail.locator(".suggestion-loading-spinner").first
        expect(spinner).to_have_attribute("aria-label", "Running")
        expect(spinner).to_have_text("")
        assert spinner.evaluate("e => getComputedStyle(e).maskImage.includes('loading.spinner.svg')")
        assert spinner.evaluate("e => getComputedStyle(e).animationName") == "ticker-suggestion-loading"

        page.locator("[data-execution-session-id=primary]").click()
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
        page.locator('.agent-session-mode-combobox [data-agent-combobox-trigger]').click()
        page.locator('.agent-session-mode-combobox [data-agent-combobox-option="new"]').click()
        expect(page.locator("[data-agent-session-capacity]")).to_have_text("2")
        expect(page.locator("#agent_response_output")).to_be_hidden()
        ask = page.get_by_role("button", name="Ask ChatGPT Web", exact=True)
        expect(ask).to_be_disabled()
        prompt = page.locator("#agent_prompt_input")
        prompt.fill("A separate draft")
        page.locator("[data-execution-session-id=second]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Prompt second")
        page.locator('.agent-session-mode-combobox [data-agent-combobox-trigger]').click()
        page.locator('.agent-session-mode-combobox [data-agent-combobox-option="new"]').click()
        expect(prompt).to_have_value("A separate draft")
        page.locator("[data-execution-session-id=second]").click()
        if width < 900:
            page.locator("#sidebar_toggle").click()
        page.get_by_role("button", name="Stop Agent task", exact=True).click()
        expect(page.locator("[data-agent-session-capacity]")).to_have_text("1")
        assert stopped == ["second"]
        assert agents["primary"]["running"]
        if width < 900:
            page.locator("#sidebar_toggle").click()
        geometry = rail.evaluate("e => ({width: e.clientWidth, scroll: e.scrollWidth})")
        assert geometry["scroll"] <= geometry["width"] + 1
        page.locator('.agent-session-mode-combobox [data-agent-combobox-trigger]').click()
        page.locator('.agent-session-mode-combobox [data-agent-combobox-option="new"]').click()
        expect(ask).to_be_enabled()
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1138, 390])
def test_recent_session_switch_preserves_stable_row_nodes(
    disposable_browser, sidebar_server_url, width,
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    agents = {}
    for key, revision in (("primary", 102), ("second", 101)):
        agents[key] = deepcopy(base["agent"])
        agents[key].update(
            session_id=key,
            conversation_url=f"https://chatgpt.com/c/{key}",
            running=False,
            phase="finished",
            run_id=key,
            run_revision=revision,
            session_title=f"Task {key}",
            prompt=f"Prompt {key}",
            response=f"Answer {key}",
            response_html=f"<p>Answer {key}</p>",
            history=[],
            activity=[],
        )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        route.fulfill(json={
            **base,
            "agent": agents.get(key, {"session_id": "new"}),
            "sessions": [dict(agent) for agent in agents.values()],
            "active_count": 0,
            "concurrency_limit": 2,
        })

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "logged_in": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": {"recent_sessions": [], "projects": []},
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json={
        "recent_sessions": [],
        "projects": [],
    }))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        session_list = page.locator("[data-agent-execution-session-list]")
        rows = session_list.locator(":scope > .agent-execution-session-row")
        expect(rows).to_have_count(2)
        initial_height = session_list.evaluate("element => element.getBoundingClientRect().height")
        page.evaluate("""
            () => {
                const list = document.querySelector('[data-agent-execution-session-list]');
                [...list.children].forEach((row, index) => {
                    row.dataset.identityProbe = `stable-${index}`;
                });
                window.__recentSessionChildMutations = [];
                new MutationObserver((records) => {
                    records.forEach((record) => {
                        window.__recentSessionChildMutations.push({
                            added: record.addedNodes.length,
                            removed: record.removedNodes.length,
                        });
                    });
                }).observe(list, {childList: true});
            }
        """)

        second = page.locator("[data-execution-session-id=second]")
        second.click()
        expect(page.locator("#agent_response_question")).to_have_text("Prompt second")
        expect(second).to_have_attribute("aria-pressed", "true")
        assert page.evaluate(
            "document.activeElement === document.querySelector('[data-execution-session-id=second]')"
        )
        assert rows.evaluate_all("elements => elements.map(row => row.dataset.identityProbe)") == [
            "stable-0",
            "stable-1",
        ]
        assert page.evaluate("window.__recentSessionChildMutations") == []
        final_height = session_list.evaluate("element => element.getBoundingClientRect().height")
        assert abs(final_height - initial_height) < 0.1
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1138, 390])
def test_new_session_explains_workspace_block_and_keeps_draft_until_unblocked(
    disposable_browser, sidebar_server_url, width
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    blocked = [True]
    blocked_reason = (
        "This workspace already has an active write-capable Agent task. "
        "Wait for it to finish or stop it before starting another write-capable task."
    )
    completed = {
        **base["agent"],
        "session_id": "selected",
        "run_id": "selected-run",
        "workspace_path": "/tmp/shared-workspace",
        "session_title": "Completed task",
        "running": False,
        "phase": "finished",
    }
    active = {
        **completed,
        "session_id": "active",
        "run_id": "active-run",
        "session_title": "Active writer",
        "running": True,
        "phase": "running",
    }
    page.add_init_script(
        "sessionStorage.setItem('cachelikes:agent-execution-session:edge:chatgpt', 'selected')"
    )

    def status(route):
        session_id = route.request.headers.get("x-cachelikes-agent-session", "selected")
        if not blocked[0]:
            active.update(running=False, phase="finished")
        route.fulfill(json={
            **base,
            "agent": {"session_id": "new"} if session_id == "new" else completed,
            "sessions": [completed, active],
            "active_count": 1 if blocked[0] else 0,
            "can_start": not blocked[0],
            "start_blocked_reason": blocked_reason if blocked[0] else "",
        })

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "logged_in": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions(),
    }))
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        expect(page.locator("[data-execution-session-id=selected]")).to_have_attribute(
            "aria-pressed", "true"
        )
        page.locator("[data-agent-new-session]").click()

        status_copy = page.locator("#agent_response_status")
        ask = page.get_by_role("button", name="Ask ChatGPT Web", exact=True)
        expect(status_copy).to_have_attribute("data-status", "loading")
        expect(status_copy).to_contain_text(f"Waiting · {blocked_reason}")
        expect(ask).to_be_disabled()
        expect(ask).to_have_attribute("title", blocked_reason)
        expect(ask).to_have_attribute("aria-describedby", "agent_response_status")

        prompt = page.locator("#agent_prompt_input")
        prompt.fill("Keep this draft while the active writer finishes.")
        blocked[0] = False
        expect(ask).to_be_enabled(timeout=5_000)
        expect(status_copy).to_have_attribute("data-status", "ready")
        expect(status_copy).to_contain_text("Ready")
        expect(prompt).to_have_value("Keep this draft while the active writer finishes.")
        expect(ask).to_have_attribute("title", "Ask ChatGPT Web")
        expect(ask).not_to_have_attribute("aria-describedby", "agent_response_status")
    finally:
        context.close()


def test_failed_local_session_without_remote_record_has_centered_hover_delete(
    disposable_browser, sidebar_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1026, "height": 1090})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    remote_url = "https://chatgpt.com/c/remote-failure"
    sessions = [
        {"session_id": "orphan", "session_title": "Failed before remote record",
         "running": False, "phase": "failed", "conversation_url": ""},
        {"session_id": "remote", "session_title": "Failed with remote record",
         "running": False, "phase": "failed", "conversation_url": remote_url},
        {"session_id": "complete", "session_title": "Completed local task",
         "running": False, "phase": "finished", "conversation_url": ""},
    ]
    base.update(agent={"session_id": "new"}, sessions=sessions, active_count=0)
    deleted = []

    def delete_session(route):
        deleted.append(route.request.post_data_json)
        route.fulfill(json={"deleted": True, "session_id": "orphan"})

    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route("**/api/agent/session", delete_session)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": {
            "projects": [],
            "recent_sessions": [{"url": remote_url, "title": "Remote failure"}],
        },
    }))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        summary = page.locator("[data-agent-execution-sessions] > summary")
        assert summary.bounding_box()["height"] == pytest.approx(36, abs=0.1)
        expect(page.locator("[data-agent-new-session]")).to_be_visible()
        assert page.locator("[data-agent-execution-sessions]").evaluate(
            "(rail) => rail.contains(document.querySelector('[data-agent-new-session]'))",
        )

        orphan = page.locator("[data-execution-session-id=orphan]")
        remote = page.locator("[data-execution-session-id=remote]")
        complete = page.locator("[data-execution-session-id=complete]")
        expect(orphan.locator("xpath=..").locator(".agent-execution-session-delete")).to_have_count(1)
        expect(remote.locator("xpath=..").locator(".agent-execution-session-delete")).to_have_count(0)
        expect(complete.locator("xpath=..").locator(".agent-execution-session-delete")).to_have_count(0)

        orphan.hover()
        delete_button = orphan.locator("xpath=..").locator(".agent-execution-session-delete")
        expect(delete_button).to_be_visible()
        expect(orphan.locator(".agent-execution-session-state")).to_have_css("opacity", "0")
        centers = orphan.locator("xpath=..").evaluate("""row => {
            const pill = row.querySelector('.agent-execution-session').getBoundingClientRect();
            const action = row.querySelector('.agent-execution-session-delete').getBoundingClientRect();
            return {
                pillRightCenterX: pill.right - pill.height / 2,
                pillCenterY: pill.top + pill.height / 2,
                actionCenterX: action.left + action.width / 2,
                actionCenterY: action.top + action.height / 2,
            };
        }""")
        assert centers["actionCenterX"] == pytest.approx(centers["pillRightCenterX"], abs=0.1)
        assert centers["actionCenterY"] == pytest.approx(centers["pillCenterY"], abs=0.1)

        delete_button.click()
        expect(page.locator("[data-execution-session-id=orphan]")).to_have_count(0)
        assert deleted == [{
            "session_id": "orphan",
            "conversation_url": "",
            "remote_record_absent": True,
        }]
    finally:
        context.close()


@pytest.mark.parametrize(("width", "color_scheme"), [(1024, "light"), (390, "dark")])
def test_agent_sidebar_trailing_controls_share_the_toggle_centerline(
    disposable_browser, sidebar_server_url, width, color_scheme,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1_164},
        color_scheme=color_scheme,
    )
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    project_url = "https://chatgpt.com/g/g-p-demo/project"
    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "logged_in": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": {
            "recent_sessions": [],
            "projects": [{"url": project_url, "title": "Demo project"}],
        },
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json={
        "recent_sessions": [],
        "projects": [{"url": project_url, "title": "Demo project"}],
    }))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        source = page.locator(".agent-session-mode-combobox")
        source.locator("[data-agent-combobox-trigger]").click()
        source.locator('[data-agent-combobox-option="project"]').click()
        expect(page.locator("[data-agent-project-field]")).to_be_visible()

        sidebar_geometry = page.evaluate("""() => {
            const bounds = selector => document.querySelector(selector).getBoundingClientRect();
            const centerX = rect => rect.left + rect.width / 2;
            const centerY = rect => rect.top + rect.height / 2;
            const sidebar = bounds('#agent_sidebar');
            const toggle = bounds('#sidebar_toggle');
            const chooser = bounds('#agent_project_path_choose');
            const summary = document.querySelector('[data-agent-execution-sessions] > summary');
            const summaryRect = summary.getBoundingClientRect();
            const summaryStyle = getComputedStyle(summary);
            const trailingTrackWidth = Number.parseFloat(
                summaryStyle.gridTemplateColumns.split(' ').at(-1),
            );
            const newSession = bounds('[data-agent-new-session]');
            const newSessionIcon = bounds('.agent-new-session-icon');
            return {
                chooserCenterDelta: Math.abs(centerX(chooser) - centerX(toggle)),
                collapseCenterDelta: Math.abs(
                    summaryRect.right - trailingTrackWidth / 2 - centerX(toggle),
                ),
                iconLeftCircleDelta: Math.abs(
                    centerX(newSessionIcon) - (newSession.left + newSession.height / 2),
                ),
                pillChevronCenters: Array.from(
                    document.querySelectorAll('#agent_runtime_form .browser-picker-trigger-chevron'),
                    chevron => centerX(chevron.getBoundingClientRect()),
                ),
                toggleEdgeDelta: Math.abs(
                    centerY(toggle) - sidebar.top - (sidebar.right - centerX(toggle)),
                ),
                newSessionIconMask: getComputedStyle(
                    document.querySelector('.agent-new-session-icon'),
                ).maskImage,
            };
        }""")
        assert sidebar_geometry["toggleEdgeDelta"] <= 1
        assert sidebar_geometry["chooserCenterDelta"] <= 1
        assert sidebar_geometry["collapseCenterDelta"] <= 1
        assert sidebar_geometry["iconLeftCircleDelta"] <= 1
        assert "plus.svg" in sidebar_geometry["newSessionIconMask"]
        assert max(sidebar_geometry["pillChevronCenters"]) - min(
            sidebar_geometry["pillChevronCenters"]
        ) <= 1
    finally:
        context.close()


@pytest.mark.parametrize(("width", "color_scheme"), [(1024, "light"), (390, "dark")])
def test_new_session_inherits_selected_project_without_stopping_existing_task(
    disposable_browser, sidebar_server_url, width, color_scheme,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1_164},
        color_scheme=color_scheme,
    )
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    project_url = "https://chatgpt.com/g/g-p-demo/project"
    conversation_url = "https://chatgpt.com/c/project-session"
    selected_agent = {
        **base["agent"],
        "session_id": "selected",
        "run_id": "selected-run",
        "running": True,
        "phase": "running",
        "workspace_path": "/tmp/demo-project",
        "project_url": project_url,
        "conversation_url": conversation_url,
        "session_title": "Existing project task",
    }
    stopped = []
    submitted = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def status(route):
        session_id = route.request.headers.get("x-cachelikes-agent-session", "new")
        agent = selected_agent if session_id == "selected" else {"session_id": "new"}
        route.fulfill(json={
            **base,
            "agent": agent,
            "sessions": [selected_agent],
            "active_count": 1,
            "can_start": True,
        })

    page.route("**/api/agent/status", status)
    page.route("**/api/agent/preferences", lambda route: route.fulfill(json=base))
    page.route("**/api/agent/stop", lambda route: (stopped.append(True), route.fulfill(json=base)))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "logged_in": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": {
            "recent_sessions": [],
            "projects": [{"url": project_url, "title": "Demo project"}],
        },
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json={
        "recent_sessions": [],
        "projects": [{"url": project_url, "title": "Demo project"}],
    }))
    page.route("**/api/agent/project-sessions**", lambda route: route.fulfill(json={
        "sessions": [{"url": conversation_url, "title": "Existing project task"}],
    }))
    page.route("**/api/agent/chatgpt-session-history**", lambda route: route.fulfill(json={"history": []}))

    def ask(route):
        submitted.append(route.request.post_data_json)
        route.fulfill(json={
            **base,
            "agent": {**base["agent"], "session_id": "created", "running": True, "phase": "running"},
            "sessions": [selected_agent],
            "active_count": 1,
            "can_start": True,
        })

    page.route("**/api/agent/ask", ask)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        source = page.locator(".agent-session-mode-combobox")
        source.locator("[data-agent-combobox-trigger]").click()
        source.locator('[data-agent-combobox-option="project"]').click()
        projects = page.locator('[data-agent-session-list="projects"]')
        projects.locator("[data-agent-combobox-trigger]").click()
        projects.get_by_role("option", name="Demo project", exact=True).click()
        page.locator("[data-execution-session-id=selected]").click()
        expect(page.locator("#agent_response_question")).to_have_text(selected_agent["prompt"])

        prompt = page.locator("#agent_prompt_input")
        prompt.fill("Start a separate task in the same project.")
        new_session = page.locator("[data-agent-new-session]")
        expect(new_session).to_have_class("secondary-button agent-new-session-button")
        placement = new_session.evaluate("""button => {
            const form = document.querySelector('#agent_runtime_form');
            const recent = document.querySelector('[data-agent-execution-sessions]');
            return {
                inForm: form.contains(button),
                inRecent: recent.contains(button),
                beforeList: Boolean(button.compareDocumentPosition(
                    document.querySelector('[data-agent-execution-session-list]'),
                ) & Node.DOCUMENT_POSITION_FOLLOWING),
            };
        }""")
        assert placement == {"inForm": False, "inRecent": True, "beforeList": True}
        alignment = new_session.evaluate("""button => {
            const body = button.closest('.ui-collapse-body');
            const buttonRect = button.getBoundingClientRect();
            const bodyRect = body.getBoundingClientRect();
            return {
                display: getComputedStyle(body).display,
                rightDelta: Math.abs(bodyRect.right - buttonRect.right),
            };
        }""")
        assert alignment["display"] == "grid"
        assert alignment["rightDelta"] <= 1
        new_session.click()

        expect(page.locator('input[name="session_mode"]')).to_have_value("project_new")
        expect(page.locator('input[name="project_url"]')).to_have_value(project_url)
        expect(page.locator('input[name="conversation_url"]')).to_have_value("")
        expect(projects.locator("[data-agent-combobox-selected-label]")).to_have_text("Demo project")
        expect(prompt).to_have_value("")
        expect(page.locator("#agent_response_output")).to_be_hidden()
        expect(page.locator("[data-execution-session-id=selected]")).to_have_attribute("aria-pressed", "false")
        assert not stopped

        page.locator("[data-execution-session-id=selected]").click()
        expect(prompt).to_have_value("Start a separate task in the same project.")
        new_session.click()
        expect(prompt).to_have_value("")
        prompt.fill("Start the inherited project task.")
        if width < 900:
            page.locator("#sidebar_toggle").click()

        ask_button = page.get_by_role("button", name="Ask ChatGPT Web", exact=True)
        expect(ask_button).to_be_enabled()
        with page.expect_response("**/api/agent/ask"):
            ask_button.click()
        expect(page.get_by_role("button", name="Stop Agent task", exact=True)).to_be_visible()
        assert len(submitted) == 1
        assert submitted[0]["session_mode"] == "project_new"
        assert submitted[0]["project_url"] == project_url
        assert submitted[0]["conversation_url"] == ""
        assert submitted[0]["prompt"] == "Start the inherited project task."
        assert not stopped
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
        page.locator("[data-execution-session-id=primary]").click()
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


def test_chatgpt_receipt_marker_is_scoped_to_the_latest_user_root(
    disposable_browser,
):
    from app.core import computer_use_agent as agent

    context = disposable_browser.new_context()
    page = context.new_page()
    marker = "agent-turn-0123456789abcdef0123456789abcdef"
    other_marker = "agent-turn-fedcba9876543210fedcba9876543210"

    def turn_html(latest_marker: str) -> str:
        return f"""
            <div data-message-author-role="user" data-message-id="old-user">
              Controller turn receipt: {marker}
            </div>
            <div data-message-author-role="assistant" data-message-id="old-assistant">
              Prior response containing {marker}
            </div>
            <div data-message-author-role="user" data-message-id="latest-user">
              Controller turn receipt: {latest_marker}
            </div>
            <div data-message-author-role="assistant" data-message-id="latest-assistant">
              <pre><code>{{"action":"bodycheck"}}</code></pre>
            </div>
            <div id="prompt-textarea" contenteditable="true">{marker}</div>
        """

    try:
        page.set_content(turn_html(other_marker))
        stale_snapshot = agent._provider_turn_snapshot(
            page,
            "chatgpt",
            receipt_marker=marker,
        )
        assert stale_snapshot["latestUserMessageId"] == "latest-user"
        assert stale_snapshot["markerEchoed"] is False

        page.set_content(turn_html(marker))
        current_snapshot = agent._provider_turn_snapshot(
            page,
            "chatgpt",
            receipt_marker=marker,
        )
        assert current_snapshot["latestUserMessageId"] == "latest-user"
        assert current_snapshot["markerEchoed"] is True
        assert current_snapshot["assistantAfterLatestUser"] is True
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
        capacity = page.locator("[data-agent-session-capacity]")
        expect(capacity).to_have_text("1")
        expect(capacity).to_have_attribute("aria-label", "1 active session")
        expect(page.locator(".agent-execution-session-title")).to_have_text("Official conversation title")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1042, 390])
def test_project_filter_keeps_same_title_cross_workspace_active_sessions(
    disposable_browser,
    sidebar_server_url,
    width,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 863},
        reduced_motion="reduce",
    )
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    project_urls = {
        "primary": "https://chatgpt.com/g/g-p-primary/project",
        "second": "https://chatgpt.com/g/g-p-second/project",
    }
    workspaces = {
        "primary": "/tmp/project-alpha",
        "second": "/tmp/project-beta",
    }
    shared_title = "审计这个项目的代码复用状况"
    agents = {
        key: {
            **base["agent"],
            "session_id": key,
            "run_id": key,
            "run_revision": 100,
            "workspace_path": workspaces[key],
            "project_url": project_urls[key],
            "conversation_url": f"https://chatgpt.com/c/{key}",
            "session_mode": "project_session",
            "session_title": shared_title,
            "prompt": shared_title,
            "running": True,
            "phase": "running",
            "finished_at": "",
            "history": [],
        }
        for key in ("primary", "second")
    }
    finished_elsewhere = {
        **agents["second"],
        "session_id": "finished",
        "run_id": "finished",
        "conversation_url": "https://chatgpt.com/c/finished",
        "session_title": "Finished elsewhere",
        "running": False,
        "phase": "finished",
    }
    page.add_init_script(
        "sessionStorage.setItem("
        "'cachelikes:agent-execution-session:edge:chatgpt', 'primary')"
    )

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        route.fulfill(
            json={
                **base,
                "agent": agents.get(key, agents["primary"]),
                "sessions": [*agents.values(), finished_elsewhere],
                "active_count": 2,
                "can_start": False,
                "concurrency_limit": 2,
            }
        )

    catalog = {
        "recent_sessions": [],
        "projects": [
            {"url": project_urls[key], "title": f"Project {key}"}
            for key in ("primary", "second")
        ],
    }
    page.route("**/api/agent/status", status)
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "can_download": True,
                "logged_in": True,
                "browser": "edge",
                "platform": "chatgpt",
                "agent_sources": catalog,
            }
        ),
    )
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog))
    page.route(
        "**/api/agent/project-sessions**",
        lambda route: route.fulfill(json={"sessions": []}),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        capacity = page.locator("[data-agent-session-capacity]")
        expect(capacity).to_have_text("2")
        expect(capacity).to_have_attribute("aria-label", "2 active sessions")
        expect(page.locator(".agent-execution-session-title")).to_have_text(
            [shared_title, shared_title]
        )
        expect(page.locator("[data-execution-session-id=primary]")).to_be_visible()
        expect(page.locator("[data-execution-session-id=second]")).to_be_visible()
        expect(page.locator("[data-execution-session-id=finished]")).to_have_count(0)

        page.locator("[data-execution-session-id=second]").click()
        expect(page.locator("[data-agent-project-name]")).to_have_text("project-beta")
        expect(page.locator('input[name="workspace_path"]')).to_have_value(
            workspaces["second"]
        )
        expect(capacity).to_have_text("2")
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
    page.add_init_script(
        """(() => {
            const key = 'cachelikes:agent-execution-session:edge:chatgpt';
            if (!sessionStorage.getItem(key)) sessionStorage.setItem(key, 'primary');
        })()"""
    )
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
    page.route("**/api/agent/project-sessions**", lambda route: route.fulfill(json={"sessions": []}))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        for index, key in enumerate(("second", "primary", "second")):
            if index:
                source = page.locator(".agent-session-mode-combobox")
                source.locator("[data-agent-combobox-trigger]").click()
                source.locator('[data-agent-combobox-option="new"]').click()
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


@pytest.mark.parametrize("width", [1024, 390])
@pytest.mark.parametrize("phase", ["finished", "failed"])
def test_session_diagnostics_stay_collapsed_and_pager_above_composer(disposable_browser, sidebar_server_url, width, phase):
    color_scheme = "dark" if width == 390 else "light"
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1164},
        color_scheme=color_scheme,
    )
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    agent = base["agent"]
    agent.update(session_id="primary", run_id="selected-run", phase=phase, running=False,
        context_file="/tmp/already-cleaned-context.md", event_chain_state="valid",
        last_error="ChatGPT turn timed out." if phase == "failed" else "",
        error_traceback=(
            "Traceback (most recent call last):\nRuntimeError: ChatGPT turn timed out."
            if phase == "failed" else ""
        ), response="Final answer", response_html="<p>Final answer</p>",
        history=[{"prompt": f"Question {i}", "response": f"Answer {i}",
                  "response_html": f"<p>Answer {i}</p>"} for i in range(1, 57)])
    base.update(sessions=[dict(agent)], active_count=0, can_start=True)
    requests = []

    def doctor(route):
        requests.append(route.request.url)
        route.fulfill(json={"run_id": "selected-run", "status": "attention", "checks": [],
            "events": [{"kind": "page.observation", "detail": "Internal diagnostic"}] * 80,
            "actions": ([{"id": "continue", "label": "Continue timed-out task",
                          "description": "Continue in the same conversation.", "enabled": True}]
                        if phase == "failed" else [])})

    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route("**/api/agent/doctor", doctor)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": {"recent_sessions": [], "projects": []}}))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        pager = page.locator("#agent_response_pagination")
        expect(pager).to_be_visible()
        panel = page.locator("#agent_doctor_panel")
        if phase == "finished":
            expect(panel).to_be_hidden()
            assert requests == []
        else:
            expect(panel).to_be_visible()
            expect(panel).to_have_js_property("open", False)
            expect(page.locator("#agent_doctor_status")).to_have_text("Can continue")
            technical = page.locator("#agent_error_record")
            expect(technical).not_to_have_attribute("hidden", "")
            expect(technical).to_have_js_property("open", False)
            expect(page.locator(".agent-workspace-content > #agent_error_record")).to_have_count(0)
            warning_style = panel.evaluate('''element => {
                const panelStyle = getComputedStyle(element);
                const summary = element.querySelector("summary");
                const summaryStyle = getComputedStyle(summary);
                const affordanceStyle = getComputedStyle(summary, "::after");
                return {
                    backgroundColor: panelStyle.backgroundColor,
                    borderColor: panelStyle.borderColor,
                    boxShadow: panelStyle.boxShadow,
                    summaryColor: summaryStyle.color,
                    maskImage: affordanceStyle.maskImage,
                    transform: affordanceStyle.transform,
                };
            }''')
            assert warning_style["backgroundColor"] == "rgba(244, 197, 66, 0.12)"
            assert warning_style["borderColor"] == "rgb(244, 197, 66)"
            assert warning_style["boxShadow"] != "none"
            expected_summary_color = "rgb(244, 197, 66)" if color_scheme == "dark" else "rgb(107, 82, 0)"
            assert warning_style["summaryColor"] == expected_summary_color
            assert warning_style["maskImage"] != "none"
            panel.locator(":scope > summary").click()
            expect(panel).to_have_js_property("open", True)
            expect(page.get_by_role("button", name="Continue timed-out task")).to_be_visible()
            expect(technical).to_be_visible()
            technical.locator(":scope > summary").click()
            expect(technical).to_have_js_property("open", True)
            expect(page.locator("#agent_error_record_content")).to_contain_text(
                "RuntimeError: ChatGPT turn timed out."
            )
            page.wait_for_timeout(220)
            open_transform = panel.locator(":scope > summary").evaluate(
                'summary => getComputedStyle(summary, "::after").transform',
            )
            assert open_transform != warning_style["transform"]
            panel.locator(":scope > summary").click()
            expect(panel).to_have_js_property("open", False)
        bounds = page.evaluate('''() => {
            const pager = document.querySelector('#agent_response_pagination').getBoundingClientRect();
            const composer = document.querySelector('.agent-composer-shell').getBoundingClientRect();
            return {bottom:pager.bottom, top:composer.top};
        }''')
        assert bounds["bottom"] <= bounds["top"]
        assert bounds["top"] - bounds["bottom"] < 100
    finally:
        context.close()


def test_remote_history_does_not_inherit_local_session_doctor(
    disposable_browser,
    sidebar_server_url,
) -> None:
    context = disposable_browser.new_context(viewport={"width": 1024, "height": 1164})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    local_url = "https://chatgpt.com/c/local-failed-session"
    remote_url = "https://chatgpt.com/c/remote-audit-session"
    local_agent = {
        **base["agent"],
        "session_id": "primary",
        "run_id": "local-failed-run",
        "phase": "failed",
        "running": False,
        "conversation_url": local_url,
        "conversation_bound": True,
        "last_error": "Local task failed.",
    }
    doctor_requests = []

    page.route(
        "**/api/agent/status",
        lambda route: route.fulfill(
            json={
                **base,
                "agent": local_agent,
                "sessions": [local_agent],
                "active_count": 0,
                "can_start": True,
            }
        ),
    )

    def doctor(route):
        doctor_requests.append(route.request.url)
        route.fulfill(
            json={
                "run_id": "local-failed-run",
                "status": "attention",
                "checks": [],
                "events": [],
                "actions": [],
            }
        )

    page.route("**/api/agent/doctor", doctor)
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "can_download": True,
                "browser": "edge",
                "platform": "chatgpt",
                "agent_sources": {
                    "recent_sessions": [
                        {"url": remote_url, "title": "Remote audit"},
                    ],
                    "projects": [],
                },
            }
        ),
    )
    page.route(
        "**/api/agent/chatgpt-session-history**",
        lambda route: route.fulfill(
            json={
                "title": "Remote audit",
                "history": [
                    {
                        "prompt": "Review the project architecture.",
                        "response": "Audit completed without local changes.",
                        "response_html": "<p>Audit completed without local changes.</p>",
                    }
                ],
            }
        ),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        panel = page.locator("#agent_doctor_panel")
        expect(panel).to_be_visible()
        assert len(doctor_requests) == 1

        page.locator(
            f'[data-recent-conversation-url="{remote_url}"]'
        ).click()

        expect(page.locator("#agent_response_question")).to_have_text(
            "Review the project architecture."
        )
        expect(panel).to_be_hidden()
        expect(page.locator("#agent_error_record")).to_be_hidden()
        page.wait_for_timeout(250)
        assert len(doctor_requests) == 1
    finally:
        context.close()


@pytest.mark.parametrize('width', [1024, 390])
@pytest.mark.parametrize('project', [False, True])
@pytest.mark.parametrize('start_new', [False, True])
def test_completed_session_accepts_followup_in_composer(disposable_browser, sidebar_server_url, width, project, start_new):
    context = disposable_browser.new_context(viewport={'width': width, 'height': 1100})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    agent = base['agent']
    agent.update(session_id='primary', run_id='completed-run', phase='finished', running=False,
        workspace_path='/tmp/followup', browser='edge', platform='chatgpt',
        conversation_bound=True, conversation_url='https://chatgpt.com/c/existing',
        project_url='https://chatgpt.com/g/g-p-existing/project' if project else '',
        session_title='Completed task')
    other = {**agent, 'session_id': 'second', 'run_id': 'other-run', 'session_title': 'Other completed task'}
    base.update(sessions=[dict(agent), other], active_count=0, can_start=True)
    submitted = []
    page.route('**/api/agent/status', lambda route: route.fulfill(json={**base, 'agent':
        {'session_id': 'new'} if route.request.headers.get('x-cachelikes-agent-session') == 'new'
        else other if route.request.headers.get('x-cachelikes-agent-session') == 'second' else agent}))
    page.route('**/api/browser-session**', lambda route: route.fulfill(json={
        'can_download': True, 'browser': 'edge', 'platform': 'chatgpt',
        'agent_sources': {'recent_sessions': [], 'projects': []}}))

    def ask(route):
        submitted.append((route.request.headers['x-cachelikes-agent-session'], route.request.post_data_json))
        route.fulfill(json={**base, 'agent': {**other, 'running': True, 'phase': 'running', 'run_id': 'followup'}})

    page.route('**/api/agent/ask', ask)
    try:
        page.goto(f'{sidebar_server_url}/agent/edge/chatgpt')
        if width < 900:
            page.locator('#sidebar_toggle').click()
        page.locator('[data-execution-session-id=second]').click()
        expect(page.locator('[data-execution-session-id=second]')).to_have_attribute('aria-pressed', 'true')
        if start_new:
            page.locator('.agent-session-mode-combobox [data-agent-combobox-trigger]').click()
            page.locator('.agent-session-mode-combobox [data-agent-combobox-option="new"]').click()
        if width < 900:
            page.locator('#sidebar_toggle').click()
        page.get_by_placeholder('Do anything', exact=True).fill('Continue with the next step.')
        page.get_by_role('button', name='Ask ChatGPT Web', exact=True).click()
        expect(page.get_by_role('button', name='Stop Agent task', exact=True)).to_be_visible()
        assert len(submitted) == 1
        session, payload = submitted[0]
        assert session == ('new' if start_new else 'second')
        assert payload['prompt'] == 'Continue with the next step.'
        assert payload['conversation_url'] == ('' if start_new else agent['conversation_url'])
        assert payload['project_url'] == ('' if start_new else agent['project_url'])
        assert payload['workspace_path'] == '/tmp/followup'
        assert payload['session_mode'] == ('new' if start_new else 'project_session' if project else 'recent')
    finally:
        context.close()
