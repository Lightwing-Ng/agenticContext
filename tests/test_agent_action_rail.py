"""Reduced-motion Agent rails commit their geometry together during resize.

Code version: v1.0.0-codex.0
"""

import pytest
from playwright.sync_api import expect

from tests import test_sidebar_e2e as fixtures


disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url


def _rail_snapshot(action):
    return action.evaluate("""button => {
        const toolbar = button.closest('.agent-response-toolbar');
        const theme = document.querySelector('#global_theme_toggle');
        const layoutNodes = [
            document.querySelector('main.agent-page'),
            document.querySelector('.agent-response-question-header'),
            document.querySelector('.agent-response-answer-shell'),
        ];
        return {
            border: getComputedStyle(toolbar).borderWidth,
            right: button.getBoundingClientRect().right,
            theme: theme.getBoundingClientRect().right,
            layoutTransitions: layoutNodes.flatMap(node => node.getAnimations()
                .filter(animation => animation instanceof CSSTransition
                    && /^(padding|margin)-/.test(animation.transitionProperty))
                .map(animation => animation.transitionProperty)),
        };
    }""")


@pytest.mark.parametrize("width", [390, 1_159])
@pytest.mark.parametrize("touch", [False, True])
def test_reduced_motion_agent_action_rail_commits_both_resize_directions(
    disposable_browser, sidebar_server_url, width, touch
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 863}, reduced_motion="reduce",
        has_touch=touch, is_mobile=touch,
    )
    page = context.new_page()
    payload = fixtures._finished_chatgpt_agent_payload()
    payload["agent"]["prompt"] = "Inspect this synthetic response. " * 30
    page.route("**/api/agent/status", lambda route: route.fulfill(json=payload))
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "platform": "chatgpt", "browser": "edge", "logged_in": True,
                "can_download": True, "agent_sources": fixtures._chatgpt_catalog_sessions(),
            }
        ),
    )
    try:
        page.goto(sidebar_server_url + "/agent/edge/chatgpt", wait_until="load")
        action = page.locator("[data-agent-open-conversation]")
        expect(action).to_be_visible()
        measurements = _rail_snapshot(action)
        assert measurements["border"] == "0px"
        assert abs(measurements["right"] - measurements["theme"]) <= 1, (
            measurements
        )
        page.set_viewport_size({"width": 1_159 if width == 390 else 390, "height": 863})
        resize = _rail_snapshot(action)
        assert not resize["layoutTransitions"], resize
        assert abs(resize["right"] - resize["theme"]) <= 1, resize
    finally:
        context.close()
